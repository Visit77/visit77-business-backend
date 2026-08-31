from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
import re

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from booking.add_on_templates import validate_configuration_values
from booking.models import (
    AddOn,
    Booking,
    BookingAddOn,
    BookingRoom,
    BookingRoomNight,
    DailyInventory,
    DailyRate,
    Guest,
    Hotel,
    Invoice,
    InvoiceLine,
    Payment,
    PhysicalRoom,
    PhysicalRoomActionHistory,
    PhysicalRoomBlock,
    RatePlan,
    RatePeriod,
    RoomAssignment,
    RoomType,
)


def stay_dates(check_in, check_out):
    current = check_in
    while current < check_out:
        yield current
        current += timedelta(days=1)


def format_money(amount, currency):
    amount = Decimal(amount or 0)
    if amount == amount.to_integral_value():
        formatted_amount = f"{int(amount):,}"
    else:
        formatted_amount = f"{amount:,.2f}"
    return f"{currency} {formatted_amount}"


def inventory_window_dates(start_date=None, days=None):
    """Return the rolling inventory dates this service keeps ready for booking."""
    start_date = start_date or timezone.localdate()
    days = settings.BOOKING_INVENTORY_WINDOW_DAYS if days is None else days
    return [start_date + timedelta(days=offset) for offset in range(days + 1)]


def active_sellable_room_count(room_type):
    return room_type.physical_rooms.filter(is_active=True).exclude(
        status=PhysicalRoom.Status.OUT_OF_SERVICE,
    ).count()


def active_ota_sellable_room_count(room_type):
    return room_type.physical_rooms.filter(
        is_active=True,
        ota_enabled=True,
        ota_sale_open=True,
    ).exclude(status=PhysicalRoom.Status.OUT_OF_SERVICE).count()


def sellable_room_count_for_date(room_type, stay_date, base_total=None):
    base_total = active_sellable_room_count(room_type) if base_total is None else base_total
    blocked_rooms = PhysicalRoomBlock.objects.filter(
        physical_room__room_type=room_type,
        physical_room__is_active=True,
        is_active=True,
        start_date__lte=stay_date,
        end_date__gte=stay_date,
    )
    blocked_rooms = blocked_rooms.values("physical_room_id").distinct().count()
    return max(base_total - blocked_rooms, 0)


def ota_sellable_room_count_for_date(room_type, stay_date):
    eligible_room_ids = room_type.physical_rooms.filter(
        is_active=True,
        ota_enabled=True,
        ota_sale_open=True,
    ).exclude(
        status=PhysicalRoom.Status.OUT_OF_SERVICE,
    ).values_list("id", flat=True)
    blocked_room_ids = PhysicalRoomBlock.objects.filter(
        physical_room_id__in=eligible_room_ids,
        is_active=True,
        start_date__lte=stay_date,
        end_date__gte=stay_date,
    ).values_list("physical_room_id", flat=True)
    assigned_room_ids = RoomAssignment.objects.filter(
        physical_room_id__in=eligible_room_ids,
        released_at__isnull=True,
        booking_room__booking__status__in=[
            Booking.Status.PENDING_PAYMENT,
            Booking.Status.CONFIRMED,
            Booking.Status.CHECKED_IN,
        ],
        booking_room__booking__check_in__lte=stay_date,
        booking_room__booking__check_out__gt=stay_date,
    ).values_list("physical_room_id", flat=True)
    return PhysicalRoom.objects.filter(id__in=eligible_room_ids).exclude(
        id__in=blocked_room_ids,
    ).exclude(id__in=assigned_room_ids).count()


@transaction.atomic
def ensure_daily_inventory_for_room_type(room_type, start_date=None, days=None, total_rooms=None):
    """Create/update future DailyInventory rows for one RoomType.

    Existing rows are kept consistent with the current physical-room count but
    never reduced below rooms that are already held or reserved.
    """
    room_type = RoomType.objects.select_for_update().get(pk=room_type.pk)
    dates = inventory_window_dates(start_date=start_date, days=days)
    total_rooms = active_sellable_room_count(room_type) if total_rooms is None else total_rooms

    existing = {
        row.stay_date: row
        for row in DailyInventory.objects.select_for_update().filter(room_type=room_type, stay_date__in=dates)
    }
    missing_dates = [day for day in dates if day not in existing]
    DailyInventory.objects.bulk_create([
        DailyInventory(
            room_type=room_type,
            stay_date=day,
            total_rooms=sellable_room_count_for_date(room_type, day, total_rooms),
        )
        for day in missing_dates
    ], ignore_conflicts=True)

    updated = 0
    for row in existing.values():
        committed_rooms = row.held_rooms + row.reserved_rooms
        date_total_rooms = sellable_room_count_for_date(room_type, row.stay_date, total_rooms)
        safe_total_rooms = max(date_total_rooms, committed_rooms)
        if row.total_rooms != safe_total_rooms:
            row.total_rooms = safe_total_rooms
            row.save(update_fields=["total_rooms"])
            updated += 1

    if room_type.default_inventory != total_rooms:
        room_type.default_inventory = total_rooms
        room_type.save(update_fields=["default_inventory"])

    return {
        "room_type_id": room_type.id,
        "total_rooms": total_rooms,
        "created": len(missing_dates),
        "updated": updated,
        "start_date": dates[0],
        "end_date": dates[-1],
    }


def ensure_rolling_daily_inventory(days=None):
    """Maintain the rolling future inventory window for all active synced room types."""
    summary = {"room_types": 0, "created": 0, "updated": 0}
    queryset = RoomType.objects.filter(hotel__is_active=True, core_active=True, booking_enabled=True).select_related("hotel")
    for room_type in queryset.iterator():
        result = ensure_daily_inventory_for_room_type(room_type, days=days)
        summary["room_types"] += 1
        summary["created"] += result["created"]
        summary["updated"] += result["updated"]
    return summary


@transaction.atomic
def reconcile_daily_inventory_for_room_type(room_type, check_in, check_out):
    """Rebuild held/reserved counters from active bookings for a stay range."""
    dates = list(stay_dates(check_in, check_out))
    if not dates:
        return []
    ensure_daily_inventory_for_room_type(
        room_type,
        start_date=dates[0],
        days=len(dates) - 1,
    )
    rows = list(DailyInventory.objects.select_for_update().filter(
        room_type=room_type, stay_date__in=dates,
    ).order_by("stay_date"))
    active_rooms = list(BookingRoom.objects.filter(
        room_type=room_type,
        booking__status__in=[
            Booking.Status.PENDING_PAYMENT,
            Booking.Status.CONFIRMED,
            Booking.Status.CHECKED_IN,
        ],
        booking__check_in__lt=check_out,
        booking__check_out__gt=check_in,
    ).select_related("booking"))
    base_total = active_sellable_room_count(room_type)
    for row in rows:
        held_rooms = sum(
            booking_room.quantity
            for booking_room in active_rooms
            if booking_room.booking.status == Booking.Status.PENDING_PAYMENT
            and booking_room.booking.check_in <= row.stay_date < booking_room.booking.check_out
        )
        reserved_rooms = sum(
            booking_room.quantity
            for booking_room in active_rooms
            if booking_room.booking.status in [Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN]
            and booking_room.booking.check_in <= row.stay_date < booking_room.booking.check_out
        )
        sellable_total = sellable_room_count_for_date(room_type, row.stay_date, base_total)
        row.held_rooms = held_rooms
        row.reserved_rooms = reserved_rooms
        row.total_rooms = max(sellable_total, held_rooms + reserved_rooms)
        row.save(update_fields=["held_rooms", "reserved_rooms", "total_rooms"])
    return rows


def _period_by_date(rate_plan, dates):
    if not dates:
        return {}
    periods = RatePeriod.objects.filter(
        rate_plan=rate_plan,
        is_active=True,
        start_date__lte=dates[-1],
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=dates[0])).order_by("start_date")
    return {
        day: next(
            (period for period in periods if period.start_date <= day and (period.end_date is None or period.end_date >= day)),
            None,
        )
        for day in dates
    }


def _display_amount(base_amount, usd_amount, display_currency):
    if display_currency == "USD" and usd_amount is not None:
        return usd_amount
    return base_amount


def _meal_plan_link_payload(link, guest_market="local", display_currency=None):
    meal_plan = link.meal_plan
    if guest_market == RatePlan.GuestMarket.FOREIGN:
        base_price = link.effective_foreign_base_price
        usd_display_price = link.effective_foreign_usd_display_price
    else:
        base_price = link.effective_local_base_price
        usd_display_price = link.effective_local_usd_display_price
    selected_display_currency = display_currency or link.room_type.hotel.base_currency
    return {
        "id": link.id,
        "meal_plan_id": meal_plan.id,
        "core_meal_plan_id": meal_plan.core_meal_plan_id,
        "name": meal_plan.name,
        "description": meal_plan.description,
        "included_meals": meal_plan.included_meals,
        "meal_windows": meal_plan.meal_windows,
        "availability": meal_plan.availability,
        "is_included": link.is_included,
        "is_default": link.is_default,
        "is_guest_selectable": link.is_guest_selectable,
        "use_hotel_default_price": link.use_hotel_default_price,
        "pricing_mode": link.pricing_mode,
        "base_currency": link.room_type.hotel.base_currency,
        "display_currency": selected_display_currency,
        "base_price": base_price,
        "usd_display_price": usd_display_price,
        "display_price": _display_amount(base_price, usd_display_price, selected_display_currency),
    }


def _meal_plan_prices(link, guest_market):
    if guest_market == RatePlan.GuestMarket.FOREIGN:
        return link.effective_foreign_base_price, link.effective_foreign_usd_display_price
    return link.effective_local_base_price, link.effective_local_usd_display_price


def _resolve_booking_meal_plan(room_type, requested):
    meal_plan_link_id = requested.get("meal_plan_link_id")
    links = room_type.meal_plan_links.select_related("meal_plan").filter(meal_plan__core_active=True)
    if meal_plan_link_id:
        link = links.filter(id=meal_plan_link_id).first()
        if not link:
            raise ValidationError({"rooms": f"Selected meal plan is unavailable for {room_type.name}."})
        if not link.is_included and not link.is_guest_selectable:
            raise ValidationError({"rooms": f"{link.meal_plan.name} cannot be selected by guests."})
        return link
    return links.filter(is_default=True, is_included=True).order_by("id").first()


def _meal_plan_booking_snapshot(link, guest_market, display_currency=None):
    if not link:
        return {}
    base_price, usd_display_price = _meal_plan_prices(link, guest_market)
    selected_display_currency = display_currency or link.room_type.hotel.base_currency
    charge_base_price = Decimal("0") if link.is_included else base_price
    charge_usd_display_price = Decimal("0") if link.is_included and usd_display_price is not None else usd_display_price
    return {
        "id": link.id,
        "meal_plan_id": link.meal_plan_id,
        "core_meal_plan_id": link.meal_plan.core_meal_plan_id,
        "name": link.meal_plan.name,
        "description": link.meal_plan.description,
        "included_meals": link.meal_plan.included_meals,
        "meal_windows": link.meal_plan.meal_windows,
        "availability": link.meal_plan.availability,
        "is_included": link.is_included,
        "is_default": link.is_default,
        "is_guest_selectable": link.is_guest_selectable,
        "pricing_mode": link.pricing_mode,
        "base_currency": link.room_type.hotel.base_currency,
        "display_currency": selected_display_currency,
        "base_price": str(base_price),
        "usd_display_price": str(usd_display_price) if usd_display_price is not None else None,
        "display_price": str(_display_amount(base_price, usd_display_price, selected_display_currency)),
        "charge_base_price": str(charge_base_price),
        "charge_usd_display_price": str(charge_usd_display_price) if charge_usd_display_price is not None else None,
    }


def _meal_plan_nightly_total(link, guest_market, quantity):
    if not link or link.is_included:
        return Decimal("0")
    base_price, _usd_display_price = _meal_plan_prices(link, guest_market)
    return base_price * quantity


def _resolve_booking_breakfast(room_type, requested, guest_market):
    plan_type = room_type.breakfast_plan_type
    included = plan_type == RoomType.BreakfastPlanType.INCLUDED_IN_ROOM_PRICE
    selected = included or requested.get("breakfast_selected", False)
    if requested.get("breakfast_selected") and plan_type == RoomType.BreakfastPlanType.NO_BREAKFAST:
        raise ValidationError({"rooms": f"Breakfast is unavailable for {room_type.name}."})
    plan = None
    if plan_type in {
        RoomType.BreakfastPlanType.HOTEL_DEFAULT_PRICE,
    }:
        plan = room_type.hotel.meal_plans.filter(
            is_default_for_room_type_breakfast=True,
            core_active=True,
        ).first()
        if not plan:
            raise ValidationError({"rooms": f"The default breakfast plan is unavailable for {room_type.name}."})
    unit_price = Decimal("0")
    usd_display_price = None
    if selected and plan_type == RoomType.BreakfastPlanType.HOTEL_DEFAULT_PRICE:
        if guest_market == RatePlan.GuestMarket.FOREIGN:
            unit_price, usd_display_price = plan.foreign_base_price, plan.foreign_usd_display_price
        else:
            unit_price, usd_display_price = plan.local_base_price, plan.local_usd_display_price
    elif selected and plan_type == RoomType.BreakfastPlanType.CUSTOM_PRICE:
        if guest_market == RatePlan.GuestMarket.FOREIGN:
            unit_price = room_type.breakfast_custom_foreign_base_price
            usd_display_price = room_type.breakfast_custom_foreign_usd_display_price
        else:
            unit_price = room_type.breakfast_custom_local_base_price
            usd_display_price = room_type.breakfast_custom_local_usd_display_price
    snapshot = {
        "type": plan_type,
        "selected": selected,
        "included": included,
        "meal_plan_id": plan.id if plan else None,
        "core_meal_plan_id": plan.core_meal_plan_id if plan else None,
        "name": plan.name if plan else "Breakfast",
        "meal_windows": plan.meal_windows if plan else {},
        "base_price": str(unit_price),
        "usd_display_price": str(usd_display_price) if usd_display_price is not None else None,
    }
    return snapshot, unit_price


def _rate_amounts(rule, rate_plan):
    if rule is None:
        return rate_plan.base_price, rate_plan.usd_display_price
    return rule.base_price, rule.usd_display_price


def room_type_booking_options(room_type):
    snapshot = room_type.core_snapshot or {}
    return {
        "allow_guest_bed_preference": snapshot.get("allow_guest_bed_preference", False),
        "allow_guest_view_preference": snapshot.get("allow_guest_view_preference", False),
        "allow_guest_bath_preference": snapshot.get("allow_guest_bath_preference", False),
        "allow_guest_smoking_preference": snapshot.get("allow_guest_smoking_preference", False),
        "supports_smoking": snapshot.get("supports_smoking", False),
        "supports_non_smoking": snapshot.get("supports_non_smoking", True),
        "beds": snapshot.get("beds", []) or [],
        "view_options": snapshot.get("view_options", []) or [],
        "bath_options": snapshot.get("bath_options", []) or [],
        "custom_options": snapshot.get("custom_options", []) or [],
    }


def room_type_extra_bed_config(room_type):
    snapshot = room_type.core_snapshot or {}
    available = bool(snapshot.get("extra_bed_available", False))
    try:
        count = max(int(snapshot.get("extra_bed_quantity") or 0), 0)
    except (TypeError, ValueError):
        count = 0
    if not available:
        count = 0
    return {"extra_bed_available": available and count > 0, "extra_bed_quantity": count}


def validate_requested_extra_beds(room_type, extra_beds, room_quantity):
    config = room_type_extra_bed_config(room_type)
    maximum = config["extra_bed_quantity"] * room_quantity
    if extra_beds > maximum:
        if maximum == 0:
            raise ValidationError({
                "rooms": f"Extra beds are unavailable for {room_type.name}."
            })
        raise ValidationError({
            "rooms": (
                f"{room_type.name} allows at most {maximum} extra bed(s) "
                f"for {room_quantity} selected room(s)."
            )
        })
    return maximum


def _decimal(value):
    return Decimal(str(value or "0"))


def _nested_id(payload, relation_name):
    if not isinstance(payload, dict):
        return None
    relation = payload.get(relation_name)
    if isinstance(relation, dict):
        return relation.get("id")
    return payload.get(f"{relation_name}_id")


def _option_total_payload(option, nights, quantity):
    extra_base_price = _decimal(option.get("extra_base_price"))
    extra_usd_display_price = option.get("extra_usd_display_price")
    return {
        "id": option.get("id"),
        "extra_base_price": str(extra_base_price),
        "extra_usd_display_price": str(extra_usd_display_price) if extra_usd_display_price is not None else None,
        "is_guest_selectable": bool(option.get("is_guest_selectable", True)),
        "is_guaranteed": bool(option.get("is_guaranteed", False)),
        "total_base_price": str(extra_base_price * nights * quantity),
    }


def resolve_room_preferences(room_type, preferences, nights, quantity):
    """Validate guest preferences against synced Core RoomType options and calculate upgrade totals."""
    preferences = preferences or {}
    snapshot = room_type.core_snapshot or {}
    selected = {}
    constraints = {}
    option_total = Decimal("0")

    def select_option(kind, flag_name, option_list_name, relation_name, submitted_id):
        nonlocal option_total
        if submitted_id in [None, ""]:
            return
        if not snapshot.get(flag_name, False):
            raise ValidationError({"preferences": f"{kind} preference is not enabled for {room_type.name}."})
        option = next(
            (
                item for item in snapshot.get(option_list_name, []) or []
                if _nested_id(item, relation_name) == submitted_id
            ),
            None,
        )
        if not option:
            raise ValidationError({"preferences": f"Selected {kind} option is not available for {room_type.name}."})
        if not option.get("is_guest_selectable", True):
            raise ValidationError({"preferences": f"Selected {kind} option is not selectable by guests."})
        option_total += _decimal(option.get("extra_base_price")) * nights * quantity
        selected[kind] = {
            **_option_total_payload(option, nights, quantity),
            "core_value_id": submitted_id,
            "name": (option.get(relation_name) or {}).get("name", ""),
        }
        if option.get("is_guaranteed", False):
            constraints[kind] = submitted_id

    select_option(
        "bed",
        "allow_guest_bed_preference",
        "beds",
        "bed_type",
        preferences.get("core_bed_type_id"),
    )
    select_option(
        "view",
        "allow_guest_view_preference",
        "view_options",
        "room_view",
        preferences.get("core_room_view_id"),
    )
    select_option(
        "bath",
        "allow_guest_bath_preference",
        "bath_options",
        "bath_type",
        preferences.get("core_bath_type_id"),
    )

    smoking_type = preferences.get("smoking_type")
    if smoking_type:
        # if not snapshot.get("allow_guest_smoking_preference", False):
        #     raise ValidationError({"preferences": f"Smoking preference is not enabled for {room_type.name}."})
        # if smoking_type == "smoking" and not snapshot.get("supports_smoking", False):
        #     raise ValidationError({"preferences": f"{room_type.name} does not support smoking rooms."})
        # if smoking_type == "non_smoking" and not snapshot.get("supports_non_smoking", True):
        #     raise ValidationError({"preferences": f"{room_type.name} does not support non-smoking rooms."})
        selected["smoking"] = {"value": smoking_type, "is_guaranteed": True}
        constraints["smoking"] = smoking_type

    custom_ids = list(dict.fromkeys(preferences.get("core_custom_option_value_ids") or []))
    if custom_ids:
        custom_options = snapshot.get("custom_options", []) or []
        for custom_id in custom_ids:
            option = next(
                (
                    item for item in custom_options
                    if _nested_id(item, "option_value") == custom_id
                ),
                None,
            )
            if not option:
                raise ValidationError({"preferences": f"Selected custom option {custom_id} is not available for {room_type.name}."})
            if not option.get("is_guest_selectable", True):
                raise ValidationError({"preferences": f"Selected custom option {custom_id} is not selectable by guests."})
            option_total += _decimal(option.get("extra_base_price")) * nights * quantity
            option_value = option.get("option_value") or {}
            selected.setdefault("custom_options", []).append({
                **_option_total_payload(option, nights, quantity),
                "core_value_id": custom_id,
                "name": option_value.get("name", ""),
                "group": (option_value.get("group") or {}).get("name", ""),
            })
            if option.get("is_guaranteed", False):
                constraints.setdefault("custom_options", []).append(custom_id)

    return {
        "requested": preferences,
        "selected": selected,
        "guaranteed_constraints": constraints,
        "has_guaranteed_constraints": bool(constraints),
        "option_total": str(option_total),
    }, option_total


def _physical_room_bed_ids(room):
    beds = (room.core_snapshot or {}).get("beds", []) or []
    return {
        _nested_id(item, "bed_type")
        for item in beds
        if _nested_id(item, "bed_type")
    }


def _physical_room_bed_preference_standards(room):
    """Return the Core-configured soft preference groups for a physical room."""
    standards = set()
    physical_room_beds = (room.core_snapshot or {}).get("beds", []) or []
    # Older synced physical-room snapshots may not have their own bed list.
    # Only then fall back to the room-type beds; do not merge them because a
    # particular physical room can have a different bed layout from its type.
    beds = physical_room_beds or (room.room_type.core_snapshot or {}).get("beds", []) or []
    for item in beds:
        bed_type = (item or {}).get("bed_type") or {}
        standard = bed_type.get("preference_standard")
        if standard:
            standards.add(standard)
    return standards


def _physical_room_custom_option_ids(room):
    options = (room.core_snapshot or {}).get("custom_option_values", []) or []
    return {
        _nested_id(item, "option_value")
        for item in options
        if _nested_id(item, "option_value")
    }


def physical_room_matches_constraints(room, constraints):
    constraints = constraints or {}
    snapshot = room.core_snapshot or {}
    if constraints.get("bed") and constraints["bed"] not in _physical_room_bed_ids(room):
        return False
    if constraints.get("view") and _nested_id(snapshot, "room_view") != constraints["view"]:
        return False
    if constraints.get("bath") and _nested_id(snapshot, "bath_type") != constraints["bath"]:
        return False
    if constraints.get("smoking") and snapshot.get("smoking_type") != constraints["smoking"]:
        return False
    custom_constraints = set(constraints.get("custom_options") or [])
    if custom_constraints and not custom_constraints.issubset(_physical_room_custom_option_ids(room)):
        return False
    return True


def ensure_preference_capacity(room_type, dates, constraints, quantity):
    if not constraints:
        return
    matching_room_ids = [
        room.id
        for room in PhysicalRoom.objects.filter(room_type=room_type, is_active=True).exclude(
            status=PhysicalRoom.Status.OUT_OF_SERVICE,
        )
        if physical_room_matches_constraints(room, constraints)
    ]
    if len(matching_room_ids) < quantity:
        raise ValidationError({"preferences": f"Not enough physical rooms match the guaranteed preferences for {room_type.name}."})

    overlapping = BookingRoom.objects.filter(
        room_type=room_type,
        booking__status__in=[Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN],
        booking__check_in__lt=dates[-1] + timedelta(days=1),
        booking__check_out__gt=dates[0],
        preference_snapshot__has_key="guaranteed_constraints",
    )
    for day in dates:
        committed = quantity
        for booking_room in overlapping:
            existing_constraints = (booking_room.preference_snapshot or {}).get("guaranteed_constraints") or {}
            if existing_constraints and all(
                physical_room_matches_constraints(room, existing_constraints)
                for room in PhysicalRoom.objects.filter(id__in=matching_room_ids)
            ):
                if booking_room.booking.check_in <= day < booking_room.booking.check_out:
                    committed += booking_room.quantity
        if committed > len(matching_room_ids):
            raise ValidationError({"preferences": f"Guaranteed preference capacity is no longer available on {day}."})


def validate_assignment_preferences(booking_room, physical_room):
    constraints = (booking_room.preference_snapshot or {}).get("guaranteed_constraints") or {}
    if constraints and not physical_room_matches_constraints(physical_room, constraints):
        raise ValidationError("The selected physical room does not match this booking room's guaranteed preferences.")


def _natural_sort_key(value):
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", str(value or ""))
    ]


def _floor_rank(value):
    raw = str(value or "").strip().upper()
    if raw in {"G", "GF", "GROUND", "GROUND FLOOR"}:
        return 0
    basement = re.fullmatch(r"B(\d+)", raw)
    if basement:
        return -int(basement.group(1))
    number = re.search(r"-?\d+", raw)
    if number:
        return int(number.group())
    return 9999


def _physical_room_assignment_sort_key(room):
    return (
        _floor_rank(room.floor),
        str(room.building or "").lower(),
        _natural_sort_key(room.room_number),
        room.id,
    )


def _preference_match_score(booking_room, physical_room):
    preference_snapshot = booking_room.preference_snapshot or {}
    selected = preference_snapshot.get("selected") or {}
    constraints = preference_snapshot.get("guaranteed_constraints") or {}
    if not selected and not constraints:
        return 0

    def preferred_value(kind, field="core_value_id"):
        if constraints.get(kind):
            return constraints[kind]
        selected_payload = selected.get(kind) or {}
        return selected_payload.get(field)

    def same_id(left, right):
        return left is not None and right is not None and str(left) == str(right)

    score = 0
    snapshot = physical_room.core_snapshot or {}
    bed_preference = preferred_value("bed")
    if bed_preference and str(bed_preference) in {str(item) for item in _physical_room_bed_ids(physical_room)}:
        score += 10
    view_preference = preferred_value("view")
    if view_preference and same_id(_nested_id(snapshot, "room_view"), view_preference):
        score += 10
    bath_preference = preferred_value("bath")
    if bath_preference and same_id(_nested_id(snapshot, "bath_type"), bath_preference):
        score += 10
    smoking_preference = preferred_value("smoking", "value")
    if smoking_preference and snapshot.get("smoking_type") == smoking_preference:
        score += 10
    custom_constraints = constraints.get("custom_options") or [
        item.get("core_value_id")
        for item in selected.get("custom_options", []) or []
        if item.get("core_value_id")
    ]
    custom_constraints = {str(item) for item in custom_constraints}
    if custom_constraints:
        matched_custom_constraints = custom_constraints.intersection(
            {str(item) for item in _physical_room_custom_option_ids(physical_room)}
        )
        score += len(matched_custom_constraints)
    return score


def _ota_preference_standard_priority(booking_room, physical_room):
    requested = (booking_room.preference_snapshot or {}).get("requested") or {}
    preference_standard = requested.get("preference_standard")
    if not preference_standard:
        return 0
    return 0 if preference_standard in _physical_room_bed_preference_standards(physical_room) else 1


def _available_physical_rooms_for_booking_room(booking_room):
    booking = booking_room.booking
    overlapping_statuses = [Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN]
    overlapping_room_ids = RoomAssignment.objects.filter(
        released_at__isnull=True,
        booking_room__booking__status__in=overlapping_statuses,
        booking_room__booking__check_in__lt=booking.check_out,
        booking_room__booking__check_out__gt=booking.check_in,
    ).values_list("physical_room_id", flat=True)
    blocked_room_ids = PhysicalRoomBlock.objects.filter(
        is_active=True,
        start_date__lt=booking.check_out,
        end_date__gte=booking.check_in,
    ).values_list("physical_room_id", flat=True)
    candidates = (
        PhysicalRoom.objects.select_for_update()
        .filter(
            hotel=booking.hotel,
            room_type=booking_room.room_type,
            is_active=True,
        )
        .exclude(status=PhysicalRoom.Status.OUT_OF_SERVICE)
        .exclude(id__in=overlapping_room_ids)
        .exclude(id__in=blocked_room_ids)
    )
    if (
        booking.source in [Booking.Source.OTA, Booking.Source.DIRECT]
        and booking.hotel.package in [Hotel.Package.OTA, Hotel.Package.OTA_PMS]
    ):
        candidates = candidates.filter(ota_enabled=True, ota_sale_open=True)
    constraints = (booking_room.preference_snapshot or {}).get("guaranteed_constraints") or {}
    return [
        room for room in candidates
        if not constraints or physical_room_matches_constraints(room, constraints)
    ]


def auto_assign_physical_rooms_for_booking(booking):
    """Assign confirmed booking rooms to vacant physical rooms.

    The assignment is intentionally a reservation-level RoomAssignment only.
    PhysicalRoom.status stays VACANT until check-in changes it to OCCUPIED.
    """
    booking = Booking.objects.select_for_update().prefetch_related("rooms__assignments").get(pk=booking.pk)
    if booking.status != Booking.Status.CONFIRMED:
        return []

    created_assignments = []
    booking_rooms = booking.rooms.select_related("room_type").order_by("id")
    for booking_room in booking_rooms:
        assigned_count = booking_room.assignments.filter(released_at__isnull=True).count()
        missing_count = max(booking_room.quantity - assigned_count, 0)
        if missing_count <= 0:
            continue

        candidates = _available_physical_rooms_for_booking_room(booking_room)
        if booking.source in [Booking.Source.OTA, Booking.Source.DIRECT]:
            # Deterministic first-fit packing prevents scattered future stays
            # from fragmenting the OTA room pool. Reuse the lowest room number
            # whenever its full requested date range is free, then try the next.
            candidates.sort(key=lambda room: (
                _ota_preference_standard_priority(booking_room, room),
                _natural_sort_key(room.room_number),
                room.id,
            ))
        else:
            candidates.sort(key=lambda room: (
                -_preference_match_score(booking_room, room),
                *_physical_room_assignment_sort_key(room),
            ))
        for room in candidates[:missing_count]:
            assignment = RoomAssignment.objects.create(
                booking_room=booking_room,
                physical_room=room,
            )
            created_assignments.append(assignment)
            if booking.source == Booking.Source.OTA:
                actor_type = PhysicalRoomActionHistory.ActorType.OTA
            elif booking.source == Booking.Source.DIRECT:
                actor_type = PhysicalRoomActionHistory.ActorType.GUEST
            else:
                actor_type = PhysicalRoomActionHistory.ActorType.SYSTEM
            PhysicalRoomActionHistory.objects.create(
                physical_room=room,
                booking=booking,
                action=PhysicalRoomActionHistory.Action.RESERVED,
                actor_type=actor_type,
                old_status=room.status,
                new_status=room.status,
                note=booking.special_request,
                metadata={
                    "assignment_id": assignment.id,
                    "check_in": str(booking.check_in),
                    "check_out": str(booking.check_out),
                    "booking_source": booking.source,
                },
            )

    return created_assignments


def _reference(prefix):
    stamp = timezone.now().strftime("%y%m%d")
    return f"{prefix}-{stamp}-{timezone.now().strftime('%H%M%S%f')[-10:]}"


def availability_for_hotels(hotels, check_in, check_out, adults=1, children=0, guest_market="local", display_currency=None):
    if check_out <= check_in:
        raise ValidationError({"check_out": "Must be after check_in."})
    dates = list(stay_dates(check_in, check_out))
    hotel_ids = [hotel.id for hotel in hotels]
    results = {hotel_id: [] for hotel_id in hotel_ids}
    room_types = list(RoomType.objects.filter(
        hotel_id__in=hotel_ids,
        booking_enabled=True,
        core_active=True,
    ).select_related("hotel").prefetch_related("meal_plan_links", "meal_plan_links__meal_plan").order_by("hotel_id", "id"))
    room_type_ids = [room_type.id for room_type in room_types]
    inventory = {
        (row.room_type_id, row.stay_date): row
        for row in DailyInventory.objects.filter(room_type_id__in=room_type_ids, stay_date__in=dates)
    }
    plans_by_room_type = defaultdict(list)
    plans = list(RatePlan.objects.filter(
        room_type_id__in=room_type_ids,
        is_active=True,
        guest_market__in=[guest_market, RatePlan.GuestMarket.ALL],
    ).order_by("room_type_id", "id"))
    for plan in plans:
        plans_by_room_type[plan.room_type_id].append(plan)
    plan_ids = [plan.id for plan in plans]
    overrides_by_plan = defaultdict(dict)
    for row in DailyRate.objects.filter(rate_plan_id__in=plan_ids, stay_date__in=dates):
        overrides_by_plan[row.rate_plan_id][row.stay_date] = row
    periods_by_plan = defaultdict(list)
    period_rows = RatePeriod.objects.filter(
        rate_plan_id__in=plan_ids,
        is_active=True,
        start_date__lte=dates[-1],
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=dates[0])).order_by("rate_plan_id", "start_date")
    for period in period_rows:
        periods_by_plan[period.rate_plan_id].append(period)

    for room_type in room_types:
        if adults > room_type.max_adults or children > room_type.max_children:
            continue
        available = min([
            inventory[(room_type.id, day)].available_rooms
            if (room_type.id, day) in inventory
            else room_type.default_inventory
            for day in dates
        ], default=0)
        if room_type.hotel.package in [Hotel.Package.OTA, Hotel.Package.OTA_PMS]:
            # DailyInventory is shared PMS capacity. Public OTA sales are
            # additionally limited to the hotel's selected/open OTA room pool.
            available = min(
                available,
                min(
                    [ota_sellable_room_count_for_date(room_type, day) for day in dates],
                    default=0,
                ),
            )
        room_plans = []
        for plan in plans_by_room_type[room_type.id]:
            overrides = overrides_by_plan[plan.id]
            matching_periods = periods_by_plan[plan.id]
            periods = {
                day: next((period for period in matching_periods if period.start_date <= day and (period.end_date is None or period.end_date >= day)), None)
                for day in dates
            }
            arrival_rule = overrides.get(check_in) or periods.get(check_in)
            if arrival_rule and arrival_rule.closed_to_arrival:
                continue
            departure_day = check_out - timedelta(days=1)
            departure_rule = overrides.get(departure_day) or periods.get(departure_day)
            if departure_rule and departure_rule.closed_to_departure:
                continue
            effective_rules = list(overrides.values()) + [period for day, period in periods.items() if day not in overrides and period]
            if any(rule.min_stay > len(dates) for rule in effective_rules):
                continue
            nightly = []
            for day in dates:
                rule = overrides.get(day) or periods.get(day)
                base_price, usd_price = _rate_amounts(rule, plan)
                selected_display_currency = display_currency or room_type.hotel.base_currency
                nightly.append({
                    "date": str(day),
                    "base_price": base_price,
                    "usd_display_price": usd_price,
                    "display_currency": selected_display_currency,
                    "display_price": _display_amount(base_price, usd_price, selected_display_currency),
                    "price": base_price,
                })
            room_plans.append({
                "id": plan.id,
                "code": plan.code,
                "name": plan.name,
                "guest_market": plan.guest_market,
                "is_default": plan.is_default,
                "base_currency": room_type.hotel.base_currency,
                "display_currency": display_currency or room_type.hotel.base_currency,
                "currency": room_type.hotel.base_currency,
                "default_base_price": plan.base_price,
                "default_usd_display_price": plan.usd_display_price,
                "default_display_price": _display_amount(
                    plan.base_price,
                    plan.usd_display_price,
                    display_currency or room_type.hotel.base_currency,
                ),
                "nightly_prices": nightly,
                "base_total": sum((item["base_price"] for item in nightly), Decimal("0")),
                "display_total": sum((item["display_price"] for item in nightly), Decimal("0")),
                "total": sum((item["base_price"] for item in nightly), Decimal("0")),
                "extra_bed_base_price": plan.extra_bed_base_price,
                "extra_bed_usd_display_price": plan.extra_bed_usd_display_price,
                "extra_bed_display_price": _display_amount(plan.extra_bed_base_price, plan.extra_bed_usd_display_price, display_currency or room_type.hotel.base_currency),
                "extra_bed_price": plan.extra_bed_base_price,
                "breakfast_included": plan.breakfast_included,
                "refundable": plan.refundable,
                "cancellation_policy": plan.cancellation_policy,
            })
        if room_plans and available > 0:
            breakfast, _breakfast_unit_price = _resolve_booking_breakfast(
                room_type,
                {"breakfast_selected": room_type.breakfast_plan_type in {
                    RoomType.BreakfastPlanType.HOTEL_DEFAULT_PRICE,
                    RoomType.BreakfastPlanType.CUSTOM_PRICE,
                }},
                guest_market,
            )
            breakfast["selectable"] = room_type.breakfast_plan_type in {
                RoomType.BreakfastPlanType.HOTEL_DEFAULT_PRICE,
                RoomType.BreakfastPlanType.CUSTOM_PRICE,
            }
            snapshot = room_type.core_snapshot or {}
            hotel_cancellation_policy = (
                (room_type.hotel.core_snapshot or {}).get("hotel_cancellation_policy")
                or snapshot.get("hotel_cancellation_policy")
            )
            room_cancellation_policy = snapshot.get("room_cancellation_policy")
            effective_cancellation_policy = (
                snapshot.get("effective_cancellation_policy")
                or snapshot.get("cancellation_policy")
                or hotel_cancellation_policy
            )
            extra_bed_config = room_type_extra_bed_config(room_type)
            default_rate_plan = next(
                (plan for plan in room_plans if plan["is_default"]),
                room_plans[0],
            )
            if not effective_cancellation_policy and default_rate_plan:
                effective_cancellation_policy = default_rate_plan.get("cancellation_policy")
                if not room_cancellation_policy:
                    hotel_cancellation_policy = hotel_cancellation_policy or effective_cancellation_policy
            results[room_type.hotel_id].append({
                "room_type_id": room_type.id,
                "core_room_type_id": room_type.core_room_type_id,
                "name": room_type.name,
                "description": room_type.description,
                "cover_image_url": room_type.cover_image_url,
                "photos": snapshot.get("photos") or [],
                "amenities": snapshot.get("amenities") or [],
                "facilities": snapshot.get("facilities") or [],
                "policies": snapshot.get("policies") or [],
                "hotel_cancellation_policy": hotel_cancellation_policy,
                "room_cancellation_policy": room_cancellation_policy,
                "cancellation_policy": effective_cancellation_policy,
                "bed_type": snapshot.get("bed_type"),
                "beds": snapshot.get("beds") or [],
                "room_standard": snapshot.get("room_standard"),
                "room_build_type": snapshot.get("room_build_type"),
                "room_view": snapshot.get("room_view"),
                "room_area": snapshot.get("room_area"),
                "room_area_from": snapshot.get("room_area_from"),
                "room_area_to": snapshot.get("room_area_to"),
                "area_unit": snapshot.get("area_unit"),
                "size_sqft": snapshot.get("size_sqft"),
                "default_prices": {
                    "local": {
                        "base_price": snapshot.get("local_base_price"),
                        "base_currency": snapshot.get("local_base_currency") or room_type.hotel.base_currency,
                        "usd_display_price": snapshot.get("local_usd_display_price"),
                    },
                    "foreign": {
                        "base_price": snapshot.get("foreign_base_price"),
                        "base_currency": snapshot.get("foreign_base_currency") or room_type.hotel.base_currency,
                        "usd_display_price": snapshot.get("foreign_usd_display_price"),
                    },
                },
                "default_price": {
                    "rate_plan_id": default_rate_plan["id"],
                    "guest_market": default_rate_plan["guest_market"],
                    "base_price": default_rate_plan["default_base_price"],
                    "base_currency": default_rate_plan["base_currency"],
                    "usd_display_price": default_rate_plan["default_usd_display_price"],
                    "display_price": default_rate_plan["default_display_price"],
                    "display_currency": default_rate_plan["display_currency"],
                },
                "extra_bed_price": {
                    "rate_plan_id": default_rate_plan["id"],
                    "guest_market": default_rate_plan["guest_market"],
                    "base_price": default_rate_plan["extra_bed_base_price"],
                    "base_currency": default_rate_plan["base_currency"],
                    "usd_display_price": default_rate_plan["extra_bed_usd_display_price"],
                    "display_price": default_rate_plan["extra_bed_display_price"],
                    "display_currency": default_rate_plan["display_currency"],
                    "pricing_unit": "per_bed_per_night",
                },
                "extra_bed_base_price": default_rate_plan["extra_bed_base_price"],
                "extra_bed_usd_display_price": default_rate_plan["extra_bed_usd_display_price"],
                "extra_bed_display_price": default_rate_plan["extra_bed_display_price"],
                **extra_bed_config,
                "default_rate_plan": default_rate_plan,
                "max_adults": room_type.max_adults,
                "max_children": room_type.max_children,
                "available_rooms": available,
                "booking_options": room_type_booking_options(room_type),
                "breakfast": breakfast,
                "meal_plans": [
                    _meal_plan_link_payload(link, guest_market, display_currency)
                    for link in room_type.meal_plan_links.all()
                    if link.meal_plan.core_active
                ],
                "rate_plans": room_plans,
                "core_snapshot": room_type.core_snapshot,
            })
    return results


def availability_for_hotel(hotel, check_in, check_out, adults=1, children=0, guest_market="local"):
    return availability_for_hotel_with_display(
        hotel, check_in, check_out, adults, children, guest_market, None,
    )


def availability_for_hotel_with_display(hotel, check_in, check_out, adults=1, children=0, guest_market="local", display_currency=None):
    return availability_for_hotels(
        [hotel], check_in, check_out, adults, children, guest_market, display_currency,
    ).get(hotel.id, [])


def _lock_inventory(room_type, dates):
    for day in dates:
        DailyInventory.objects.get_or_create(
            room_type=room_type,
            stay_date=day,
            defaults={"total_rooms": room_type.default_inventory},
        )
    return list(DailyInventory.objects.select_for_update().filter(room_type=room_type, stay_date__in=dates).order_by("stay_date"))


def estimate_booking(data):
    hotel = Hotel.objects.get(core_business_id=data["core_business_id"], is_active=True)
    check_in, check_out = data["check_in"], data["check_out"]
    if check_out <= check_in:
        raise ValidationError({"check_out": "Must be after check_in."})
    dates = list(stay_dates(check_in, check_out))
    guest_market = data.get("guest_market", RatePlan.GuestMarket.LOCAL)
    room_total = Decimal("0")
    breakfast_total = Decimal("0")
    grand_total = Decimal("0")
    rooms = []
    summary_items = []
    selected_room_count = 0
    selected_extra_bed_count = 0
    for requested in data["rooms"]:
        try:
            room_type = RoomType.objects.get(
                hotel=hotel,
                core_room_type_id=requested["core_room_type_id"],
                booking_enabled=True,
                core_active=True,
            )
            rate_plan = RatePlan.objects.get(
                id=requested["rate_plan_id"],
                room_type=room_type,
                is_active=True,
                guest_market__in=[guest_market, RatePlan.GuestMarket.ALL],
            )
        except (RoomType.DoesNotExist, RatePlan.DoesNotExist):
            raise ValidationError({"rooms": "A selected room type or rate plan is unavailable."})
        quantity = requested["quantity"]
        extra_beds = requested.get("extra_beds", 0)
        validate_requested_extra_beds(room_type, extra_beds, quantity)
        preference_snapshot, option_total = resolve_room_preferences(
            room_type,
            requested.get("preferences") or {},
            len(dates),
            quantity,
        )
        meal_plan_link = _resolve_booking_meal_plan(room_type, requested)
        meal_plan_snapshot = _meal_plan_booking_snapshot(meal_plan_link, guest_market)
        breakfast_snapshot, breakfast_unit_price = _resolve_booking_breakfast(room_type, requested, guest_market)
        ensure_preference_capacity(
            room_type,
            dates,
            preference_snapshot.get("guaranteed_constraints") or {},
            quantity,
        )
        daily_rate_rows = {row.stay_date: row for row in DailyRate.objects.filter(rate_plan=rate_plan, stay_date__in=dates)}
        periods = _period_by_date(rate_plan, dates)
        nightly_option_total = (option_total / len(dates)) if dates else Decimal("0")
        nights = []
        item_total = Decimal("0")
        extra_bed_stay_total = Decimal("0")
        meal_plan_stay_total = Decimal("0")
        breakfast_stay_total = Decimal("0")
        for day in dates:
            rule = daily_rate_rows.get(day) or periods.get(day)
            unit_price, usd_display_price = _rate_amounts(rule, rate_plan)
            extra_bed_total = rate_plan.extra_bed_base_price * extra_beds
            meal_plan_night_total = _meal_plan_nightly_total(meal_plan_link, guest_market, quantity)
            breakfast_night_total = breakfast_unit_price * quantity
            night_total = unit_price * quantity + extra_bed_total + nightly_option_total + meal_plan_night_total + breakfast_night_total
            nights.append({
                "stay_date": day,
                "unit_price": unit_price,
                "usd_display_price": usd_display_price,
                "quantity": quantity,
                "extra_bed_total": extra_bed_total,
                "option_total": nightly_option_total,
                "meal_plan_total": meal_plan_night_total,
                "breakfast_total": breakfast_night_total,
                "total": night_total,
            })
            item_total += night_total
            extra_bed_stay_total += extra_bed_total
            meal_plan_stay_total += meal_plan_night_total
            breakfast_stay_total += breakfast_night_total
        room_total += item_total - breakfast_stay_total
        breakfast_total += breakfast_stay_total
        grand_total += item_total
        selected_room_count += quantity
        selected_extra_bed_count += extra_beds
        room_stay_total = item_total - extra_bed_stay_total - option_total - meal_plan_stay_total - breakfast_stay_total
        summary_items.append({
            "type": "room",
            "label": f"{quantity} x {room_type.name}",
            "room_type_id": room_type.id,
            "core_room_type_id": room_type.core_room_type_id,
            "rate_plan_id": rate_plan.id,
            "quantity": quantity,
            "amount": room_stay_total,
            "formatted_amount": format_money(room_stay_total, hotel.base_currency),
        })
        if extra_beds:
            summary_items.append({
                "type": "extra_bed",
                "label": f"{extra_beds} x Extra Bed(s)",
                "room_type_id": room_type.id,
                "core_room_type_id": room_type.core_room_type_id,
                "rate_plan_id": rate_plan.id,
                "quantity": extra_beds,
                "amount": extra_bed_stay_total,
                "formatted_amount": format_money(extra_bed_stay_total, hotel.base_currency),
            })
        if option_total:
            summary_items.append({
                "type": "option",
                "label": f"{quantity} x {room_type.name} option upgrade(s)",
                "room_type_id": room_type.id,
                "core_room_type_id": room_type.core_room_type_id,
                "rate_plan_id": rate_plan.id,
                "quantity": quantity,
                "amount": option_total,
                "formatted_amount": format_money(option_total, hotel.base_currency),
            })
        if meal_plan_stay_total:
            summary_items.append({
                "type": "meal_plan",
                "label": f"{quantity} x {meal_plan_link.meal_plan.name} x {len(dates)} Night{'s' if len(dates) != 1 else ''}",
                "room_type_id": room_type.id,
                "core_room_type_id": room_type.core_room_type_id,
                "rate_plan_id": rate_plan.id,
                "meal_plan_link_id": meal_plan_link.id,
                "meal_plan_id": meal_plan_link.meal_plan_id,
                "quantity": quantity,
                "amount": meal_plan_stay_total,
                "formatted_amount": format_money(meal_plan_stay_total, hotel.base_currency),
            })
        if breakfast_stay_total:
            summary_items.append({
                "type": "breakfast",
                "label": f"{quantity} x Breakfast x {len(dates)} Night{'s' if len(dates) != 1 else ''}",
                "room_type_id": room_type.id,
                "core_room_type_id": room_type.core_room_type_id,
                "quantity": quantity,
                "amount": breakfast_stay_total,
                "formatted_amount": format_money(breakfast_stay_total, hotel.base_currency),
            })
        rooms.append({
            "core_room_type_id": room_type.core_room_type_id,
            "room_type_id": room_type.id,
            "room_type_name": room_type.name,
            "rate_plan_id": rate_plan.id,
            "rate_plan_name": rate_plan.name,
            "quantity": quantity,
            "extra_beds": requested.get("extra_beds", 0),
            "preference_snapshot": preference_snapshot,
            "option_total": option_total,
            "meal_plan": meal_plan_snapshot,
            "meal_plan_total": meal_plan_stay_total,
            "breakfast": breakfast_snapshot,
            "breakfast_total": breakfast_stay_total,
            "nights": nights,
            "total": item_total,
            "formatted_total": format_money(item_total, hotel.base_currency),
        })
    summary_text_parts = [f"{selected_room_count} Room{'s' if selected_room_count != 1 else ''}"]
    if len(dates):
        summary_text_parts.append(f"{len(dates)} Night{'s' if len(dates) != 1 else ''}")
    if selected_extra_bed_count:
        summary_text_parts.append(f"{selected_extra_bed_count} Extra Bed{'s' if selected_extra_bed_count != 1 else ''}")
    return {
        "hotel": {
            "id": hotel.id,
            "core_business_id": hotel.core_business_id,
            "name": hotel.name,
            "base_currency": hotel.base_currency,
        },
        "check_in": check_in,
        "check_out": check_out,
        "nights": len(dates),
        "currency": hotel.base_currency,
        "guests": data.get("guests", []),
        "rooms": rooms,
        "summary_items": summary_items,
        "summary_text": " x ".join(summary_text_parts[:2]) + (
            f" x {selected_extra_bed_count} Extra Bed{'s' if selected_extra_bed_count != 1 else ''}"
            if selected_extra_bed_count else ""
        ),
        "room_total": room_total,
        "breakfast_total": breakfast_total,
        "grand_total": grand_total,
        "formatted_room_total": format_money(room_total, hotel.base_currency),
        "formatted_breakfast_total": format_money(breakfast_total, hotel.base_currency),
        "formatted_grand_total": format_money(grand_total, hotel.base_currency),
    }


@transaction.atomic
def create_booking(data, idempotency_key=None):
    hotel = Hotel.objects.get(core_business_id=data["core_business_id"], is_active=True)
    if idempotency_key:
        existing = Booking.objects.filter(hotel=hotel, idempotency_key=idempotency_key).first()
        if existing:
            ensure_initial_invoice(existing)
            return existing, False

    check_in, check_out = data["check_in"], data["check_out"]
    if check_out <= check_in:
        raise ValidationError({"check_out": "Must be after check_in."})
    dates = list(stay_dates(check_in, check_out))
    hold_expires_at = timezone.now() + timedelta(minutes=settings.BOOKING_HOLD_MINUTES)
    booking = Booking.objects.create(
        reference=_reference("BK"),
        hotel=hotel,
        core_customer_user_id=data.get("core_customer_user_id"),
        idempotency_key=idempotency_key,
        source=data.get("source", Booking.Source.DIRECT),
        source_name=data.get("source_name", ""),
        check_in=check_in,
        check_out=check_out,
        contact_name=data["contact_name"],
        contact_phone=data["contact_phone"],
        contact_email=data.get("contact_email", ""),
        guest_market=data.get("guest_market", RatePlan.GuestMarket.LOCAL),
        special_request=data.get("special_request", ""),
        hold_expires_at=hold_expires_at,
    )

    room_total = Decimal("0")
    booking_currency = hotel.base_currency
    guest_market = data.get("guest_market", RatePlan.GuestMarket.LOCAL)
    policy_snapshot = {}
    for requested in data["rooms"]:
        try:
            room_type = RoomType.objects.get(hotel=hotel, core_room_type_id=requested["core_room_type_id"], booking_enabled=True, core_active=True)
            rate_plan = RatePlan.objects.get(
                id=requested["rate_plan_id"],
                room_type=room_type,
                is_active=True,
                guest_market__in=[guest_market, RatePlan.GuestMarket.ALL],
            )
        except (RoomType.DoesNotExist, RatePlan.DoesNotExist):
            raise ValidationError({"rooms": "A selected room type or rate plan is unavailable."})
        quantity = requested["quantity"]
        validate_requested_extra_beds(
            room_type, requested.get("extra_beds", 0), quantity,
        )
        if requested.get("adults", 1) > room_type.max_adults * quantity or requested.get("children", 0) > room_type.max_children * quantity:
            raise ValidationError({"rooms": f"Guest count exceeds {room_type.name} capacity."})
        preference_snapshot, option_total = resolve_room_preferences(
            room_type,
            requested.get("preferences") or {},
            len(dates),
            quantity,
        )
        meal_plan_link = _resolve_booking_meal_plan(room_type, requested)
        meal_plan_snapshot = _meal_plan_booking_snapshot(meal_plan_link, guest_market)
        breakfast_snapshot, breakfast_unit_price = _resolve_booking_breakfast(room_type, requested, guest_market)
        ensure_preference_capacity(
            room_type,
            dates,
            preference_snapshot.get("guaranteed_constraints") or {},
            quantity,
        )
        inventory_rows = _lock_inventory(room_type, dates)
        if len(inventory_rows) != len(dates) or any(row.available_rooms < quantity for row in inventory_rows):
            raise ValidationError({"rooms": f"{room_type.name} is no longer available for all selected dates."})
        DailyInventory.objects.filter(id__in=[row.id for row in inventory_rows]).update(held_rooms=F("held_rooms") + quantity)

        booking_room = BookingRoom.objects.create(
            booking=booking,
            room_type=room_type,
            rate_plan=rate_plan,
            quantity=quantity,
            adults=requested.get("adults", 1),
            children=requested.get("children", 0),
            extra_beds=requested.get("extra_beds", 0),
            meal_plan_link=meal_plan_link,
            room_type_snapshot={"core_room_type_id": room_type.core_room_type_id, "name": room_type.name, **room_type.core_snapshot},
            rate_plan_snapshot={
                "code": rate_plan.code,
                "name": rate_plan.name,
                "base_currency": hotel.base_currency,
                "base_price": str(rate_plan.base_price),
                "usd_display_price": str(rate_plan.usd_display_price) if rate_plan.usd_display_price is not None else None,
                "extra_bed_base_price": str(rate_plan.extra_bed_base_price),
                "extra_bed_usd_display_price": str(rate_plan.extra_bed_usd_display_price) if rate_plan.extra_bed_usd_display_price is not None else None,
                "currency": hotel.base_currency,
                "refundable": rate_plan.refundable,
                "cancellation_policy": rate_plan.cancellation_policy,
            },
            meal_plan_snapshot=meal_plan_snapshot,
            breakfast_snapshot=breakfast_snapshot,
            preference_snapshot=preference_snapshot,
            option_total=option_total,
        )
        daily_rate_rows = {row.stay_date: row for row in DailyRate.objects.filter(rate_plan=rate_plan, stay_date__in=dates)}
        periods = _period_by_date(rate_plan, dates)
        arrival_rule = daily_rate_rows.get(check_in) or periods.get(check_in)
        if arrival_rule and arrival_rule.closed_to_arrival:
            raise ValidationError({"rooms": f"{rate_plan.name} is closed to arrival on {check_in}."})
        departure_night = check_out - timedelta(days=1)
        departure_rule = daily_rate_rows.get(departure_night) or periods.get(departure_night)
        if departure_rule and departure_rule.closed_to_departure:
            raise ValidationError({"rooms": f"{rate_plan.name} is closed to departure on {check_out}."})
        effective_rules = list(daily_rate_rows.values()) + [period for day, period in periods.items() if day not in daily_rate_rows and period]
        if any(rule.min_stay > len(dates) for rule in effective_rules):
            raise ValidationError({"rooms": f"{rate_plan.name} minimum-stay requirement is not met."})
        item_total = Decimal("0")
        meal_plan_stay_total = Decimal("0")
        breakfast_stay_total = Decimal("0")
        nightly_option_total = (option_total / len(dates)) if dates else Decimal("0")
        for day in dates:
            rule = daily_rate_rows.get(day) or periods.get(day)
            unit_price, _usd_display_price = _rate_amounts(rule, rate_plan)
            extra_bed_total = rate_plan.extra_bed_base_price * requested.get("extra_beds", 0)
            meal_plan_night_total = _meal_plan_nightly_total(meal_plan_link, guest_market, quantity)
            breakfast_night_total = breakfast_unit_price * quantity
            night_total = unit_price * quantity + extra_bed_total + nightly_option_total + meal_plan_night_total + breakfast_night_total
            BookingRoomNight.objects.create(
                booking_room=booking_room,
                stay_date=day,
                unit_price=unit_price,
                quantity=quantity,
                extra_bed_total=extra_bed_total,
                option_total=nightly_option_total,
                meal_plan_total=meal_plan_night_total,
                breakfast_total=breakfast_night_total,
                total=night_total,
            )
            item_total += night_total
            meal_plan_stay_total += meal_plan_night_total
            breakfast_stay_total += breakfast_night_total
        booking_room.total = item_total
        booking_room.meal_plan_total = meal_plan_stay_total
        booking_room.breakfast_total = breakfast_stay_total
        booking_room.save(update_fields=["meal_plan_total", "breakfast_total", "total"])
        room_total += item_total
        policy_snapshot[str(rate_plan.id)] = rate_plan.cancellation_policy

    add_on_total = Decimal("0")
    for requested in data.get("add_ons", []):
        try:
            add_on = AddOn.objects.select_related("template").get(id=requested["add_on_id"], hotel=hotel, is_active=True)
        except AddOn.DoesNotExist:
            raise ValidationError({"add_ons": "A selected add-on is unavailable."})
        if add_on.currency != hotel.base_currency:
            raise ValidationError({"add_ons": "Add-ons must use the hotel base currency."})
        quantity = requested.get("quantity", 1)
        configuration = requested.get("configuration", {})
        configuration_errors = validate_configuration_values(add_on.configuration_schema, configuration)
        if configuration_errors:
            raise ValidationError({"add_ons": {str(add_on.id): configuration_errors}})
        if add_on.pricing_unit == AddOn.PricingUnit.PER_BOOKING:
            multiplier = 1
        elif add_on.pricing_unit == AddOn.PricingUnit.PER_NIGHT:
            multiplier = len(dates) * quantity
        else:
            multiplier = quantity
        total = add_on.price * multiplier
        BookingAddOn.objects.create(
            booking=booking,
            add_on=add_on,
            quantity=quantity,
            unit_price=add_on.price,
            total=total,
            configuration=configuration,
            add_on_snapshot={
                "code": add_on.code,
                "name": add_on.name,
                "pricing_unit": add_on.pricing_unit,
                "service_type": add_on.service_type,
                "template_id": add_on.template_id,
                "template_version": add_on.template.version if add_on.template_id else None,
                "configuration_schema": add_on.configuration_schema,
            },
        )
        add_on_total += total

    guests = data.get("guests") or [{"name": data["contact_name"], "phone": data["contact_phone"], "email": data.get("contact_email", ""), "is_primary": True}]
    Guest.objects.bulk_create([Guest(booking=booking, **guest) for guest in guests])
    booking.currency = hotel.base_currency
    booking.room_total = room_total
    booking.add_on_total = add_on_total
    booking.grand_total = room_total + add_on_total + booking.tax_total - booking.discount_total
    booking.cancellation_policy_snapshot = policy_snapshot
    booking.save(update_fields=["currency", "room_total", "add_on_total", "grand_total", "cancellation_policy_snapshot"])
    ensure_initial_invoice(booking)
    return booking, True


def _move_inventory(booking, from_field, to_field=None):
    for room in booking.rooms.select_related("room_type"):
        dates = list(stay_dates(booking.check_in, booking.check_out))
        rows = _lock_inventory(room.room_type, dates)
        for row in rows:
            current = getattr(row, from_field)
            if current < room.quantity:
                raise ValidationError("Inventory state is inconsistent.")
            setattr(row, from_field, current - room.quantity)
            update_fields = [from_field]
            if to_field:
                setattr(row, to_field, getattr(row, to_field) + room.quantity)
                update_fields.append(to_field)
            row.save(update_fields=update_fields)


def release_checked_in_booking_inventory(booking):
    """Release room-type inventory when an active stay checks out."""
    if booking.status != Booking.Status.CHECKED_IN:
        raise ValidationError("Only a checked-in booking can release stay inventory.")
    _move_inventory(booking, "reserved_rooms")


def _sync_invoice_status(invoice):
    if invoice.status == Invoice.Status.VOID:
        return invoice
    paid_amount = invoice.paid_amount
    invoice.status = (
        Invoice.Status.PAID if invoice.total <= paid_amount
        else Invoice.Status.PARTIALLY_PAID if paid_amount > 0
        else Invoice.Status.OPEN
    )
    invoice.save(update_fields=["status", "updated_at"])
    return invoice


def create_invoice(booking, invoice_type, lines, tax_total=Decimal("0"), discount_total=Decimal("0"), note="", add_to_booking_total=False):
    normalized_lines = []
    subtotal = Decimal("0")
    for item in lines:
        quantity = Decimal(str(item.get("quantity", 1)))
        unit_price = Decimal(str(item["unit_price"]))
        line_total = quantity * unit_price
        if quantity <= 0 or unit_price < 0:
            raise ValidationError({"lines": "Quantity must be positive and unit price cannot be negative."})
        subtotal += line_total
        normalized_lines.append((item, quantity, unit_price, line_total))
    tax_total = Decimal(str(tax_total or 0))
    discount_total = Decimal(str(discount_total or 0))
    total = subtotal + tax_total - discount_total
    if not normalized_lines:
        raise ValidationError({"lines": "At least one invoice line is required."})
    if tax_total < 0 or discount_total < 0 or total < 0:
        raise ValidationError({"total": "Invoice totals cannot be negative."})
    invoice = Invoice.objects.create(
        booking=booking,
        invoice_number=_reference("IV"),
        invoice_type=invoice_type,
        currency=booking.currency,
        subtotal=subtotal,
        tax_total=tax_total,
        discount_total=discount_total,
        total=total,
        note=note,
    )
    InvoiceLine.objects.bulk_create([
        InvoiceLine(
            invoice=invoice,
            description=item["description"],
            quantity=quantity,
            unit_price=unit_price,
            total=line_total,
            metadata=item.get("metadata", {}),
        )
        for item, quantity, unit_price, line_total in normalized_lines
    ])
    if add_to_booking_total:
        booking.add_on_total += subtotal
        booking.tax_total += tax_total
        booking.discount_total += discount_total
        booking.grand_total += total
        booking.save(update_fields=["add_on_total", "tax_total", "discount_total", "grand_total", "updated_at"])
    return invoice


def _booking_charge_lines(booking):
    lines = []
    room_groups = {}
    for room in booking.rooms.select_related("room_type", "meal_plan_link__meal_plan").prefetch_related("nights"):
        nights = list(room.nights.all())
        room_price_total = sum(
            (night.unit_price * night.quantity for night in nights),
            Decimal("0"),
        )
        extra_bed_total = sum((night.extra_bed_total for night in nights), Decimal("0"))
        option_total = sum((night.option_total for night in nights), Decimal("0"))
        meal_plan_total = sum((night.meal_plan_total for night in nights), Decimal("0"))
        breakfast_total = sum((night.breakfast_total for night in nights), Decimal("0"))
        room_type_key = room.room_type_id
        group = room_groups.setdefault(room_type_key, {
            "room_type": room.room_type,
            "booking_room_ids": [],
            "quantity": 0,
            "night_count": len(nights) if nights else booking.nights,
            "room_total": Decimal("0"),
            "extra_bed_count": 0,
            "extra_bed_total": Decimal("0"),
            "option_total": Decimal("0"),
            "meal_plans": {},
            "breakfasts": {},
            "legacy_total": Decimal("0"),
        })
        group["booking_room_ids"].append(room.id)
        group["quantity"] += room.quantity

        # Legacy bookings may not have nightly component rows. Keep their original
        # single-line total so invoice synchronization never drops a charge.
        if not nights:
            group["legacy_total"] += room.total
            continue

        group["room_total"] += room_price_total
        group["extra_bed_count"] += room.extra_beds
        group["extra_bed_total"] += extra_bed_total
        group["option_total"] += option_total
        if room.meal_plan_link_id:
            meal_plan_name = room.meal_plan_snapshot.get("name") or room.meal_plan_link.meal_plan.name
            included = bool(room.meal_plan_snapshot.get("is_included"))
            plan_key = (room.meal_plan_link_id, included)
            plan = group["meal_plans"].setdefault(plan_key, {
                "name": meal_plan_name,
                "quantity": 0,
                "total": Decimal("0"),
                "meal_plan_link_id": room.meal_plan_link_id,
                "meal_plan_id": room.meal_plan_link.meal_plan_id,
                "included": included,
            })
            plan["quantity"] += room.quantity
            plan["total"] += meal_plan_total
        if room.breakfast_snapshot.get("selected"):
            breakfast_name = room.breakfast_snapshot.get("name") or "Breakfast"
            included = bool(room.breakfast_snapshot.get("included"))
            breakfast_key = (
                room.breakfast_snapshot.get("meal_plan_id"),
                breakfast_name,
                included,
            )
            breakfast = group["breakfasts"].setdefault(breakfast_key, {
                "name": breakfast_name,
                "quantity": 0,
                "total": Decimal("0"),
                "meal_plan_id": room.breakfast_snapshot.get("meal_plan_id"),
                "core_meal_plan_id": room.breakfast_snapshot.get("core_meal_plan_id"),
                "included": included,
            })
            breakfast["quantity"] += room.quantity
            breakfast["total"] += breakfast_total

    for group in room_groups.values():
        room_type = group["room_type"]
        room_name = room_type.name
        nights = group["night_count"]
        common_metadata = {
            "booking_room_id": group["booking_room_ids"][0],
            "booking_room_ids": group["booking_room_ids"],
            "room_type_id": room_type.id,
            "core_room_type_id": room_type.core_room_type_id,
        }
        room_total = group["room_total"] + group["legacy_total"]
        lines.append({
            "description": f"{room_name} x {group['quantity']} x {nights} Night{'s' if nights != 1 else ''}",
            "quantity": 1,
            "unit_price": room_total,
            "metadata": {**common_metadata, "line_type": "room"},
        })
        if group["extra_bed_total"]:
            lines.append({
                "description": (
                    f"Extra Bed for {room_name} x {group['extra_bed_count']} x "
                    f"{nights} Night{'s' if nights != 1 else ''}"
                ),
                "quantity": 1,
                "unit_price": group["extra_bed_total"],
                "metadata": {**common_metadata, "line_type": "extra_bed"},
            })
        if group["option_total"]:
            lines.append({
                "description": f"Room Preferences for {room_name}",
                "quantity": 1,
                "unit_price": group["option_total"],
                "metadata": {**common_metadata, "line_type": "room_option"},
            })
        for plan in group["meal_plans"].values():
            lines.append({
                "description": (
                    f"{plan['name']} for {room_name} x {plan['quantity']}"
                    + (" (Included in Room Price)" if plan["included"] else f" x {nights} Night{'s' if nights != 1 else ''}")
                ),
                "quantity": 1,
                "unit_price": plan["total"],
                "metadata": {
                    **common_metadata,
                    "line_type": "meal_plan",
                    "meal_plan_link_id": plan["meal_plan_link_id"],
                    "meal_plan_id": plan["meal_plan_id"],
                    "included_in_room_price": plan["included"],
                },
            })
        for breakfast in group["breakfasts"].values():
            lines.append({
                "description": (
                    f"{breakfast['name']} for {room_name} x {breakfast['quantity']}"
                    + (" (Included in Room Price)" if breakfast["included"] else f" x {nights} Night{'s' if nights != 1 else ''}")
                ),
                "quantity": 1,
                "unit_price": breakfast["total"],
                "metadata": {
                    **common_metadata,
                    "line_type": "breakfast",
                    "meal_plan_id": breakfast["meal_plan_id"],
                    "core_meal_plan_id": breakfast["core_meal_plan_id"],
                    "included_in_room_price": breakfast["included"],
                },
            })
    lines.extend({
        "description": item.add_on.name,
        "quantity": 1,
        "unit_price": item.total,
        "metadata": {"booking_add_on_id": item.id, "add_on_id": item.add_on_id, "line_type": "add_on"},
    } for item in booking.add_ons.select_related("add_on").all())
    return lines


def ensure_initial_invoice(booking):
    existing = booking.invoices.filter(invoice_type=Invoice.Type.ROOM_BOOKING).order_by("issued_at").first()
    if existing:
        return existing
    return create_invoice(
        booking,
        Invoice.Type.ROOM_BOOKING,
        _booking_charge_lines(booking),
        tax_total=booking.tax_total,
        discount_total=booking.discount_total,
        note="Original booking charges",
    )


def sync_initial_invoice(booking):
    invoice = ensure_initial_invoice(booking)
    invoice.lines.all().delete()
    lines = _booking_charge_lines(booking)
    subtotal = sum((Decimal(str(item["quantity"])) * Decimal(str(item["unit_price"])) for item in lines), Decimal("0"))
    InvoiceLine.objects.bulk_create([
        InvoiceLine(
            invoice=invoice,
            description=item["description"],
            quantity=Decimal(str(item["quantity"])),
            unit_price=Decimal(str(item["unit_price"])),
            total=Decimal(str(item["quantity"])) * Decimal(str(item["unit_price"])),
            metadata=item.get("metadata", {}),
        )
        for item in lines
    ])
    invoice.subtotal = subtotal
    invoice.tax_total = booking.tax_total
    invoice.discount_total = booking.discount_total
    invoice.total = subtotal + booking.tax_total - booking.discount_total
    invoice.save(update_fields=["subtotal", "tax_total", "discount_total", "total", "updated_at"])
    return _sync_invoice_status(invoice)


@transaction.atomic
def record_payment(booking, data, auto_assign=True):
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if (
        booking.status == Booking.Status.PENDING_PAYMENT
        and booking.hold_expires_at
        and booking.hold_expires_at <= timezone.now()
    ):
        raise ValidationError("The booking payment hold has expired.")
    invoice = None
    if data.get("invoice_id"):
        invoice = booking.invoices.filter(id=data["invoice_id"]).first()
        if not invoice:
            raise ValidationError({"invoice_id": "Invoice does not belong to this booking."})
    if invoice is None:
        ensure_initial_invoice(booking)
        invoice = next(
            (item for item in booking.invoices.prefetch_related("receipts").order_by("issued_at", "id") if item.balance > 0 and item.status != Invoice.Status.VOID),
            None,
        )
    if invoice is None:
        raise ValidationError({"invoice_id": "This booking has no outstanding invoice."})
    if data.get("status", Payment.Status.PAID) == Payment.Status.PAID:
        amount_due = invoice.balance
        if data["amount"] > amount_due:
            raise ValidationError({"amount": f"Payment exceeds invoice {invoice.invoice_number} balance of {amount_due} {booking.currency}."})
    payment = Payment.objects.create(
        booking=booking,
        invoice=invoice,
        payment_type=data.get("payment_type", Payment.Type.FULL_PAYMENT),
        provider=data["provider"],
        provider_reference=data.get("provider_reference", ""),
        status=data.get("status", Payment.Status.PAID),
        amount=data["amount"],
        currency=booking.currency,
        invoice_number=invoice.invoice_number,
        receipt_number=_reference("RC") if data.get("status", Payment.Status.PAID) == Payment.Status.PAID else None,
        metadata=data.get("metadata", {}),
        paid_at=timezone.now() if data.get("status", Payment.Status.PAID) == Payment.Status.PAID else None,
    )
    if payment.status == Payment.Status.PAID:
        was_pending = booking.status == Booking.Status.PENDING_PAYMENT
        booking.amount_paid += payment.amount
        # A successful full payment or accepted deposit commits the reservation.
        if payment.amount > 0 and was_pending:
            _move_inventory(booking, "held_rooms", "reserved_rooms")
            booking.status = Booking.Status.CONFIRMED
            booking.hold_expires_at = None
        booking.save(update_fields=["amount_paid", "status", "hold_expires_at", "updated_at"])
        if auto_assign and payment.amount > 0 and was_pending and booking.status == Booking.Status.CONFIRMED:
            auto_assign_physical_rooms_for_booking(booking)
    _sync_invoice_status(invoice)
    return payment


def _default_rate_plan_for_room_type(room_type, guest_market):
    plans = RatePlan.objects.filter(
        room_type=room_type,
        is_active=True,
        guest_market__in=[guest_market, RatePlan.GuestMarket.ALL],
    )
    return (
        plans.filter(guest_market=guest_market, is_default=True).order_by("id").first()
        or plans.filter(guest_market=RatePlan.GuestMarket.ALL, is_default=True).order_by("id").first()
        or plans.filter(guest_market=guest_market).order_by("id").first()
        or plans.filter(guest_market=RatePlan.GuestMarket.ALL).order_by("id").first()
    )


def _has_overlapping_room_assignment(physical_room, check_in, check_out, exclude_booking=None):
    overlapping_statuses = [Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN]
    queryset = RoomAssignment.objects.filter(
        physical_room=physical_room,
        released_at__isnull=True,
        booking_room__booking__status__in=overlapping_statuses,
        booking_room__booking__check_in__lt=check_out,
        booking_room__booking__check_out__gt=check_in,
    )
    if exclude_booking is not None:
        queryset = queryset.exclude(booking_room__booking=exclude_booking)
    return queryset.exists()


def _has_overlapping_room_block(physical_room, check_in, check_out):
    return PhysicalRoomBlock.objects.filter(
        physical_room=physical_room,
        is_active=True,
        start_date__lt=check_out,
        end_date__gte=check_in,
    ).exists()


def _initial_payment_payload(booking, payment_data, *, default_to_full_payment=False, source):
    """Normalize an optional create-time deposit/full-payment object."""
    if not payment_data and not default_to_full_payment:
        return None
    payment_data = dict(payment_data or {})
    payment_type = payment_data.get("payment_type") or Payment.Type.FULL_PAYMENT
    status_value = payment_data.get("status", Payment.Status.PAID)
    amount = payment_data.get("amount")
    if payment_type == Payment.Type.FULL_PAYMENT:
        amount_due = max(booking.grand_total - booking.amount_paid, Decimal("0"))
        if amount is None:
            amount = amount_due
        elif amount != amount_due:
            raise ValidationError({"payment": f"Full payment must equal {amount_due} {booking.currency}."})
    elif amount is None:
        raise ValidationError({"payment": "Amount is required for a deposit."})
    return {
        "payment_type": payment_type,
        "provider": payment_data.get("provider", Payment.Provider.CASH),
        "provider_reference": payment_data.get("provider_reference", ""),
        "status": status_value,
        "amount": amount,
        "metadata": {
            **(payment_data.get("metadata") or {}),
            "source": source,
        },
    }


@transaction.atomic
def create_walk_in_booking(data, idempotency_key=None, core_business_id=None, check_in_immediately=True):
    requested_rooms = data.get("rooms") or [{
        "physical_room_id": data["physical_room_id"],
        "rate_plan_id": data.get("rate_plan_id"),
        "meal_plan_link_id": data.get("meal_plan_link_id"),
        "breakfast_selected": data.get("breakfast_selected", False),
        "adults": data.get("adults", 1),
        "children": data.get("children", 0),
        "extra_beds": data.get("extra_beds", 0),
        "preferences": data.get("preferences") or {},
    }]
    requested_room_ids = [item["physical_room_id"] for item in requested_rooms]
    if len(requested_room_ids) != len(set(requested_room_ids)):
        raise ValidationError({"rooms": "The same physical room cannot be selected twice."})
    locked_rooms = list(PhysicalRoom.objects.select_for_update().select_related(
        "hotel", "room_type",
    ).filter(id__in=requested_room_ids, is_active=True))
    rooms_by_id = {room.id: room for room in locked_rooms}
    if len(rooms_by_id) != len(requested_room_ids):
        raise ValidationError({"rooms": "One or more selected physical rooms are unavailable."})
    physical_rooms = [rooms_by_id[room_id] for room_id in requested_room_ids]
    hotel = physical_rooms[0].hotel
    if any(room.hotel_id != hotel.id for room in physical_rooms):
        raise ValidationError({"rooms": "All selected physical rooms must belong to the same hotel."})
    if core_business_id and hotel.core_business_id != core_business_id:
        raise ValidationError({"rooms": "One or more selected physical rooms do not belong to this business."})
    allowed_statuses = [PhysicalRoom.Status.VACANT]
    if not check_in_immediately:
        # V2 first creates a reservation. An occupied room can be reserved for
        # a future non-overlapping stay, while immediate/legacy check-in must
        # still use a room that is vacant right now.
        allowed_statuses.append(PhysicalRoom.Status.OCCUPIED)
    check_in, check_out = data["check_in"], data["check_out"]
    if check_out <= check_in:
        raise ValidationError({"check_out": "Must be after check_in."})
    guest_market = data.get("guest_market", RatePlan.GuestMarket.LOCAL)
    resolved_rooms = []
    reconciled_room_type_ids = set()
    for index, (requested, physical_room) in enumerate(zip(requested_rooms, physical_rooms)):
        if physical_room.status not in allowed_statuses:
            message = (
                "Only a vacant physical room can be used for walk-in check-in."
                if check_in_immediately
                else "Only a vacant or currently occupied physical room can be reserved."
            )
            raise ValidationError({f"rooms[{index}].physical_room_id": message})
        if _has_overlapping_room_assignment(physical_room, check_in, check_out):
            raise ValidationError({
                f"rooms[{index}].physical_room_id": (
                    f"Room {physical_room.room_number} is assigned to an overlapping booking."
                )
            })
        if _has_overlapping_room_block(physical_room, check_in, check_out):
            raise ValidationError({
                f"rooms[{index}].physical_room_id": (
                    f"Room {physical_room.room_number} is blocked for one or more stay dates."
                )
            })
        if physical_room.room_type_id not in reconciled_room_type_ids:
            reconcile_daily_inventory_for_room_type(physical_room.room_type, check_in, check_out)
            reconciled_room_type_ids.add(physical_room.room_type_id)
        rate_plan_id = requested.get("rate_plan_id")
        if rate_plan_id:
            rate_plan = RatePlan.objects.filter(
                id=rate_plan_id,
                room_type=physical_room.room_type,
                is_active=True,
                guest_market__in=[guest_market, RatePlan.GuestMarket.ALL],
            ).first()
        else:
            rate_plan = _default_rate_plan_for_room_type(physical_room.room_type, guest_market)
        if not rate_plan:
            raise ValidationError({
                f"rooms[{index}].rate_plan_id": "No active rate plan is available for this physical room."
            })
        resolved_rooms.append((physical_room, rate_plan, requested))

    booking, created = create_booking(
        {
            "core_business_id": hotel.core_business_id,
            "core_customer_user_id": data.get("core_customer_user_id"),
            "source": Booking.Source.WALK_IN,
            "source_name": "Walk-in",
            "check_in": check_in,
            "check_out": check_out,
            "contact_name": data["contact_name"],
            "contact_phone": data["contact_phone"],
            "contact_email": data.get("contact_email", ""),
            "guest_market": guest_market,
            "special_request": data.get("special_request", ""),
            "rooms": [
                {
                    "core_room_type_id": physical_room.room_type.core_room_type_id,
                    "rate_plan_id": rate_plan.id,
                    "meal_plan_link_id": requested.get("meal_plan_link_id"),
                    "breakfast_selected": requested.get("breakfast_selected", False),
                    "quantity": 1,
                    "adults": requested.get("adults", 1),
                    "children": requested.get("children", 0),
                    "extra_beds": requested.get("extra_beds", 0),
                    "preferences": requested.get("preferences") or {},
                }
                for physical_room, rate_plan, requested in resolved_rooms
            ],
            "guests": data.get("guests") or [],
            "add_ons": data.get("add_ons") or [],
        },
        idempotency_key=idempotency_key,
    )
    booking = Booking.objects.select_for_update().prefetch_related("rooms").get(pk=booking.pk)
    if booking.status == Booking.Status.CHECKED_IN:
        return booking
    if not created:
        assigned_room_ids = list(
            RoomAssignment.objects.filter(
                booking_room__booking=booking,
                released_at__isnull=True,
            ).order_by("booking_room_id", "id").values_list("physical_room_id", flat=True)
        )
        if assigned_room_ids == requested_room_ids:
            return booking
        raise ValidationError({
            "rooms": "Idempotency key already belongs to a booking with a different room selection."
        })

    payment_payload = _initial_payment_payload(
        booking,
        data.get("payment"),
        default_to_full_payment=check_in_immediately,
        source="walk_in",
    )
    if payment_payload:
        payment_payload["metadata"].update({
            "physical_room_ids": [room.id for room in physical_rooms],
            "room_numbers": [room.room_number for room in physical_rooms],
        })
        if len(physical_rooms) == 1:
            # Keep the original metadata keys for existing single-room clients.
            payment_payload["metadata"].update({
                "physical_room_id": physical_rooms[0].id,
                "room_number": physical_rooms[0].room_number,
            })
    if payment_payload and payment_payload["status"] == Payment.Status.PAID:
        record_payment(booking, payment_payload, auto_assign=False)
        booking = Booking.objects.select_for_update().prefetch_related("rooms").get(pk=booking.pk)
    elif payment_payload:
        invoice = ensure_initial_invoice(booking)
        Payment.objects.create(
            booking=booking,
            invoice=invoice,
            payment_type=payment_payload["payment_type"],
            provider=payment_payload["provider"],
            provider_reference=payment_payload["provider_reference"],
            status=Payment.Status.PENDING,
            amount=payment_payload["amount"],
            currency=booking.currency,
            invoice_number=invoice.invoice_number,
            metadata=payment_payload["metadata"],
        )
        if booking.status == Booking.Status.PENDING_PAYMENT:
            _move_inventory(booking, "held_rooms", "reserved_rooms")
            booking.status = Booking.Status.CONFIRMED
            booking.hold_expires_at = None
            booking.save(update_fields=["status", "hold_expires_at", "updated_at"])
    elif booking.status == Booking.Status.PENDING_PAYMENT:
        _move_inventory(booking, "held_rooms", "reserved_rooms")
        booking.status = Booking.Status.CONFIRMED
        booking.hold_expires_at = None
        booking.save(update_fields=["status", "hold_expires_at", "updated_at"])

    booking_rooms = list(booking.rooms.select_related("room_type").order_by("id"))
    if len(booking_rooms) != len(physical_rooms):
        raise ValidationError({"rooms": "Booking room selection does not match the requested physical rooms."})
    for index, (booking_room, physical_room) in enumerate(zip(booking_rooms, physical_rooms)):
        physical_room = PhysicalRoom.objects.select_for_update().get(pk=physical_room.pk)
        if physical_room.status not in allowed_statuses:
            message = (
                "Selected physical room is no longer vacant."
                if check_in_immediately
                else "Selected physical room can no longer be reserved."
            )
            raise ValidationError({f"rooms[{index}].physical_room_id": message})
        if _has_overlapping_room_assignment(physical_room, check_in, check_out, exclude_booking=booking):
            raise ValidationError({
                f"rooms[{index}].physical_room_id": "Selected physical room is assigned to an overlapping booking."
            })
        validate_assignment_preferences(booking_room, physical_room)
        existing_assignment = booking_room.assignments.filter(released_at__isnull=True).first()
        if existing_assignment:
            if existing_assignment.physical_room_id != physical_room.id:
                raise ValidationError({"rooms": "Idempotent booking already has a different room assignment."})
        else:
            RoomAssignment.objects.create(booking_room=booking_room, physical_room=physical_room)
    if check_in_immediately:
        PhysicalRoom.objects.filter(id__in=requested_room_ids).update(status=PhysicalRoom.Status.OCCUPIED)
        booking.status = Booking.Status.CHECKED_IN
        booking.hold_expires_at = None
        booking.save(update_fields=["status", "hold_expires_at", "updated_at"])
    return booking


@transaction.atomic
def create_admin_reservation(data, idempotency_key=None, core_business_id=None):
    if core_business_id and data["core_business_id"] != core_business_id:
        raise ValidationError({"core_business_id": "Does not match X-Booking-Business-ID."})
    booking_data = dict(data)
    payment_data = booking_data.pop("payment", None)
    requested_rooms = booking_data["rooms"]
    physical_room_ids_by_index = [item.pop("physical_room_ids", []) for item in requested_rooms]
    booking, _created = create_booking(booking_data, idempotency_key=idempotency_key)
    booking = Booking.objects.select_for_update().prefetch_related("rooms").get(pk=booking.pk)

    payment_payload = _initial_payment_payload(
        booking, payment_data, default_to_full_payment=False, source="admin_reservation",
    )
    if payment_payload and payment_payload["status"] == Payment.Status.PAID:
        record_payment(booking, payment_payload, auto_assign=False)
        booking.refresh_from_db()
    elif payment_payload:
        invoice = ensure_initial_invoice(booking)
        Payment.objects.create(
            booking=booking,
            invoice=invoice,
            payment_type=payment_payload["payment_type"],
            provider=payment_payload["provider"],
            provider_reference=payment_payload["provider_reference"],
            status=Payment.Status.PENDING,
            amount=payment_payload["amount"],
            currency=booking.currency,
            invoice_number=invoice.invoice_number,
            metadata=payment_payload["metadata"],
        )
        _move_inventory(booking, "held_rooms", "reserved_rooms")
        booking.status = Booking.Status.CONFIRMED
        booking.hold_expires_at = None
        booking.save(update_fields=["status", "hold_expires_at", "updated_at"])
    elif booking.status == Booking.Status.PENDING_PAYMENT:
        _move_inventory(booking, "held_rooms", "reserved_rooms")
        booking.status = Booking.Status.CONFIRMED
        booking.hold_expires_at = None
        booking.save(update_fields=["status", "hold_expires_at", "updated_at"])

    for booking_room, physical_room_ids in zip(booking.rooms.order_by("id"), physical_room_ids_by_index):
        if len(physical_room_ids) > booking_room.quantity:
            raise ValidationError({"rooms": "Assigned physical rooms cannot exceed room quantity."})
        rooms = list(PhysicalRoom.objects.select_for_update().filter(
            id__in=physical_room_ids,
            hotel=booking.hotel,
            room_type=booking_room.room_type,
            is_active=True,
            # An occupied room may still be assigned to a future reservation
            # once its current stay has checked out. Date-range overlap below
            # is the authoritative availability check.
            status__in=[PhysicalRoom.Status.VACANT, PhysicalRoom.Status.OCCUPIED],
        ))
        if len(rooms) != len(set(physical_room_ids)):
            raise ValidationError({
                "rooms": (
                    "Every assigned physical room must be active, either vacant or occupied, "
                    "and match its room type."
                )
            })
        for room in rooms:
            if _has_overlapping_room_assignment(room, booking.check_in, booking.check_out, exclude_booking=booking):
                raise ValidationError({"rooms": f"Room {room.room_number} has an overlapping assignment."})
            if _has_overlapping_room_block(room, booking.check_in, booking.check_out):
                raise ValidationError({"rooms": f"Room {room.room_number} is blocked for one or more stay dates."})
            validate_assignment_preferences(booking_room, room)
            RoomAssignment.objects.create(booking_room=booking_room, physical_room=room)
    return booking


@transaction.atomic
def update_reservation_for_check_in(booking, data):
    """Update reservation-form fields while preserving already-booked room rates."""
    booking = Booking.objects.select_for_update().select_related("hotel").get(pk=booking.pk)
    if booking.status not in [Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED]:
        raise ValidationError("Only pending or confirmed reservations can be updated before check-in.")

    existing_rooms = list(
        booking.rooms.select_related("room_type", "rate_plan", "meal_plan_link")
        .prefetch_related("assignments")
        .order_by("id")
    )
    requested_rooms = data.get("rooms")
    if requested_rooms is None:
        requested_rooms = []
        for room in existing_rooms:
            requested_rooms.append({
                "core_room_type_id": room.room_type.core_room_type_id,
                "rate_plan_id": room.rate_plan_id,
                "meal_plan_link_id": room.meal_plan_link_id,
                "breakfast_selected": bool(room.breakfast_snapshot.get("selected")),
                "quantity": room.quantity,
                "adults": room.adults,
                "children": room.children,
                "extra_beds": room.extra_beds,
                "preferences": {},
                "physical_room_ids": [
                    assignment.physical_room_id
                    for assignment in room.assignments.all()
                    if assignment.released_at is None
                ],
            })
        if len(requested_rooms) == 1:
            for field in ["adults", "children", "extra_beds"]:
                if field in data:
                    requested_rooms[0][field] = data[field]
            if "rate_plan_id" in data:
                requested_rooms[0]["rate_plan_id"] = data["rate_plan_id"]
            if "physical_room_id" in data:
                requested_rooms[0]["physical_room_ids"] = [data["physical_room_id"]]

    assignment_ids_by_index = [list(room.pop("physical_room_ids", [])) for room in requested_rooms]
    if len(assignment_ids_by_index) != len(requested_rooms):
        raise ValidationError({"rooms": "Invalid physical room assignments."})

    existing_add_ons = [
        {"add_on_id": item.add_on_id, "quantity": item.quantity, "configuration": item.configuration}
        for item in booking.add_ons.all()
    ]
    old_inventory_field = (
        "held_rooms" if booking.status == Booking.Status.PENDING_PAYMENT else "reserved_rooms"
    )
    _move_inventory(booking, old_inventory_field)

    replacement, _created = create_booking({
        "core_business_id": booking.hotel.core_business_id,
        "core_customer_user_id": booking.core_customer_user_id,
        "source": booking.source,
        "source_name": booking.source_name,
        "check_in": data.get("check_in", booking.check_in),
        "check_out": data.get("check_out", booking.check_out),
        "contact_name": data.get("contact_name", booking.contact_name),
        "contact_phone": data.get("contact_phone", booking.contact_phone),
        "contact_email": data.get("contact_email", booking.contact_email),
        "guest_market": data.get("guest_market", booking.guest_market),
        "special_request": data.get("special_request", booking.special_request),
        "rooms": requested_rooms,
        "add_ons": data.get("add_ons", existing_add_ons),
        "guests": [{"name": data.get("contact_name", booking.contact_name), "is_primary": True}],
    })

    # A confirmed reservation is a price-locked sale.  Core may change the
    # room type/rate-plan price between reservation and check-in, but merely
    # opening or saving the check-in form must not reprice the booked nights.
    # Keep the old unit price for dates that were already present when the
    # room type and rate plan are unchanged.  Newly-added dates (an extension)
    # and deliberately changed room/rate selections retain current pricing.
    replacement_rooms = list(replacement.rooms.select_related("room_type", "rate_plan").order_by("id"))
    for old_room, new_room in zip(existing_rooms, replacement_rooms):
        if (
            old_room.room_type_id != new_room.room_type_id
            or old_room.rate_plan_id != new_room.rate_plan_id
        ):
            continue

        old_nights = {night.stay_date: night for night in old_room.nights.all()}
        changed_nights = []
        for new_night in new_room.nights.all():
            old_night = old_nights.get(new_night.stay_date)
            if old_night is None:
                continue
            new_night.unit_price = old_night.unit_price
            new_night.total = (
                (old_night.unit_price * new_night.quantity)
                + new_night.extra_bed_total
                + new_night.option_total
                + new_night.meal_plan_total
                + new_night.breakfast_total
            )
            changed_nights.append(new_night)
        if changed_nights:
            BookingRoomNight.objects.bulk_update(changed_nights, ["unit_price", "total"])
            new_room.total = sum(
                (night.total for night in new_room.nights.all()),
                Decimal("0"),
            )
            new_room.rate_plan_snapshot = old_room.rate_plan_snapshot
            new_room.save(update_fields=["total", "rate_plan_snapshot"])

    replacement.room_total = sum(
        (room.total for room in replacement_rooms),
        Decimal("0"),
    )
    replacement.tax_total = booking.tax_total
    replacement.discount_total = booking.discount_total
    replacement.grand_total = (
        replacement.room_total + replacement.add_on_total
        + replacement.tax_total - replacement.discount_total
    )
    replacement.save(update_fields=["tax_total", "discount_total", "grand_total", "updated_at"])

    if booking.amount_paid > replacement.grand_total:
        raise ValidationError({
            "grand_total": (
                f"Updated total {replacement.grand_total} {replacement.currency} is below the already-paid "
                f"amount {booking.amount_paid} {booking.currency}. Refund or correct the payment first."
            )
        })

    for index, physical_ids in enumerate(assignment_ids_by_index):
        if len(physical_ids) > replacement_rooms[index].quantity:
            raise ValidationError({"rooms": "Assigned physical rooms cannot exceed room quantity."})
        physical_rooms = list(PhysicalRoom.objects.select_for_update().filter(
            id__in=physical_ids,
            hotel=booking.hotel,
            room_type=replacement_rooms[index].room_type,
            is_active=True,
        ))
        if len(physical_rooms) != len(set(physical_ids)):
            raise ValidationError({"rooms": "Every assigned physical room must be active and match its room type."})
        for physical_room in physical_rooms:
            already_assigned_to_booking = RoomAssignment.objects.filter(
                physical_room=physical_room,
                booking_room__booking=booking,
                released_at__isnull=True,
            ).exists()
            if already_assigned_to_booking and physical_room.status == PhysicalRoom.Status.OCCUPIED:
                checked_in_conflicts = RoomAssignment.objects.filter(
                    physical_room=physical_room,
                    released_at__isnull=True,
                    booking_room__booking__status=Booking.Status.CHECKED_IN,
                ).exclude(booking_room__booking=booking).select_related("booking_room__booking")
                if not checked_in_conflicts.exists():
                    # A confirmed reservation must not make the room operationally occupied.
                    # Repair stale state left by the former walk-in flow.
                    physical_room.status = PhysicalRoom.Status.VACANT
                    physical_room.save(update_fields=["status"])
                else:
                    conflict_bookings = []
                    seen_ids = set()
                    for conflict in checked_in_conflicts:
                        conflict_booking = conflict.booking_room.booking
                        if conflict_booking.id in seen_ids:
                            continue
                        seen_ids.add(conflict_booking.id)
                        conflict_bookings.append({
                            "booking_id": str(conflict_booking.id),
                            "reference": conflict_booking.reference,
                            "status": "occupied",
                            "booking_status": conflict_booking.status,
                            "contact_name": conflict_booking.contact_name,
                            "check_in": str(conflict_booking.check_in),
                            "check_out": str(conflict_booking.check_out),
                            "physical_room_id": physical_room.id,
                            "room_number": physical_room.room_number,
                        })
                    raise ValidationError({
                        "rooms": (
                            f"Room {physical_room.room_number} is occupied by checked-in booking "
                            f"{conflict_bookings[0]['reference']}. Check out or change that booking's room first."
                        ),
                        "conflict_bookings": conflict_bookings,
                    })
            if physical_room.status != PhysicalRoom.Status.VACANT and not already_assigned_to_booking:
                raise ValidationError({
                    "rooms": f"Room {physical_room.room_number} is not vacant and cannot be newly assigned."
                })
            if physical_room.status != PhysicalRoom.Status.VACANT:
                raise ValidationError({"rooms": f"Room {physical_room.room_number} is not vacant."})
            if _has_overlapping_room_assignment(
                physical_room, replacement.check_in, replacement.check_out, exclude_booking=booking,
            ):
                raise ValidationError({"rooms": f"Room {physical_room.room_number} has an overlapping assignment."})
            if _has_overlapping_room_block(physical_room, replacement.check_in, replacement.check_out):
                raise ValidationError({"rooms": f"Room {physical_room.room_number} is blocked for one or more stay dates."})
            validate_assignment_preferences(replacement_rooms[index], physical_room)

    # The replacement currently owns held inventory. Preserve the original reservation state.
    if booking.status == Booking.Status.CONFIRMED:
        _move_inventory(replacement, "held_rooms", "reserved_rooms")

    booking.rooms.all().delete()
    booking.add_ons.all().delete()
    BookingRoom.objects.filter(booking=replacement).update(booking=booking)
    BookingAddOn.objects.filter(booking=replacement).update(booking=booking)
    for index, physical_ids in enumerate(assignment_ids_by_index):
        target_room = booking.rooms.order_by("id")[index]
        for physical_room_id in physical_ids:
            RoomAssignment.objects.create(
                booking_room=target_room, physical_room_id=physical_room_id,
            )

    for field in [
        "check_in", "check_out", "contact_name", "contact_phone", "contact_email",
        "guest_market", "special_request", "currency", "room_total", "add_on_total",
        "tax_total", "discount_total", "grand_total", "cancellation_policy_snapshot",
    ]:
        setattr(booking, field, getattr(replacement, field))
    booking.save(update_fields=[
        "check_in", "check_out", "contact_name", "contact_phone", "contact_email",
        "guest_market", "special_request", "currency", "room_total", "add_on_total",
        "tax_total", "discount_total", "grand_total", "cancellation_policy_snapshot", "updated_at",
    ])
    sync_initial_invoice(booking)
    # create_booking() creates the replacement's provisional invoice as well.
    # It is only a calculation container; the real stay keeps its own invoices.
    replacement.invoices.all().delete()
    replacement.delete()
    return booking


@transaction.atomic
def refund_payment(payment, amount, provider_reference=""):
    payment = Payment.objects.select_for_update().select_related("booking").get(pk=payment.pk)
    invoice = Invoice.objects.select_for_update().filter(pk=payment.invoice_id).first() if payment.invoice_id else None
    quote = refund_quote(payment.booking)
    if amount > quote["refundable_remaining"]:
        raise ValidationError({
            "amount": (
                f"Refund exceeds the cancellation policy allowance of "
                f"{quote['refundable_remaining']} {payment.booking.currency}."
            )
        })
    remaining = payment.amount - payment.refunded_amount
    if payment.status not in [Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED] or amount <= 0 or amount > remaining:
        raise ValidationError("Refund amount exceeds the refundable payment balance.")
    payment.refunded_amount += amount
    payment.status = Payment.Status.REFUNDED if payment.refunded_amount == payment.amount else Payment.Status.PARTIALLY_REFUNDED
    if provider_reference:
        payment.metadata = {**payment.metadata, "refund_reference": provider_reference}
    payment.save(update_fields=["refunded_amount", "status", "metadata"])
    booking = Booking.objects.select_for_update().get(pk=payment.booking_id)
    booking.amount_paid = max(booking.amount_paid - amount, Decimal("0"))
    booking.save(update_fields=["amount_paid", "updated_at"])
    if invoice:
        _sync_invoice_status(invoice)
    return payment


def _refund_percent(policy, hours_before_check_in):
    policy = policy or {}
    policy_type = policy.get("type")
    if policy_type == "free_full_refund":
        return Decimal("100")
    if policy_type != "partial_refund":
        return Decimal("0")

    less_rule = policy.get("less_than_rule") or {}
    more_rules = policy.get("more_than_rules") or []
    try:
        cutoff = Decimal(str(less_rule["hours_before_check_in"]))
        less_percent = Decimal(str(less_rule["refund_percent"]))
    except (KeyError, TypeError, ValueError):
        # Read legacy snapshots safely while old bookings are still active.
        legacy = sorted(
            policy.get("refund_rules") or [],
            key=lambda rule: Decimal(str(rule.get("within_hours", 0))),
        )
        if not legacy:
            return Decimal("0")
        cutoff = Decimal(str(legacy[0].get("within_hours", 0)))
        less_percent = Decimal(str(legacy[0].get("refund_percent", 0)))
        more_rules = [
            {
                "hours_before_check_in": rule.get("within_hours"),
                "refund_percent": rule.get("refund_percent"),
            }
            for rule in legacy
        ]
    if hours_before_check_in < cutoff:
        return less_percent
    eligible = [
        rule for rule in more_rules
        if hours_before_check_in >= Decimal(str(rule.get("hours_before_check_in", 0)))
    ]
    if not eligible:
        return less_percent
    selected = max(eligible, key=lambda rule: Decimal(str(rule["hours_before_check_in"])))
    return Decimal(str(selected.get("refund_percent", 0)))


def refund_quote(booking, at=None):
    booking = Booking.objects.select_related("hotel").prefetch_related("rooms", "payments").get(pk=booking.pk)
    at = at or timezone.now()
    check_in_time = booking.hotel.check_in_time or time.min
    check_in_at = datetime.combine(booking.check_in, check_in_time)
    if timezone.is_aware(at):
        check_in_at = timezone.make_aware(check_in_at, timezone.get_current_timezone())
    hours_before = Decimal(str((check_in_at - at).total_seconds())) / Decimal("3600")
    room_items = []
    eligible_total = Decimal("0")
    for room in booking.rooms.all():
        policy = (booking.cancellation_policy_snapshot or {}).get(str(room.rate_plan_id), {})
        percent = _refund_percent(policy, hours_before)
        amount = (room.total * percent / Decimal("100")).quantize(Decimal("0.01"))
        eligible_total += amount
        room_items.append({
            "booking_room_id": room.id,
            "rate_plan_id": room.rate_plan_id,
            "policy_type": policy.get("type", "non_refundable"),
            "refund_percent": percent,
            "eligible_amount": amount,
            "description": policy.get("description", ""),
        })
    already_refunded = sum((payment.refunded_amount for payment in booking.payments.all()), Decimal("0"))
    originally_paid = booking.amount_paid + already_refunded
    eligible_total = min(eligible_total, originally_paid)
    refundable_remaining = max(eligible_total - already_refunded, Decimal("0"))
    return {
        "booking_id": booking.id,
        "currency": booking.currency,
        "calculated_at": at,
        "check_in_at": check_in_at,
        "hours_before_check_in": max(hours_before, Decimal("0")).quantize(Decimal("0.01")),
        "eligible_refund_total": eligible_total,
        "already_refunded": already_refunded,
        "refundable_remaining": refundable_remaining,
        "rooms": room_items,
    }


@transaction.atomic
def cancel_booking(booking):
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if booking.status not in [Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED]:
        raise ValidationError("This booking cannot be canceled.")
    source = "held_rooms" if booking.status == Booking.Status.PENDING_PAYMENT else "reserved_rooms"
    _move_inventory(booking, source)
    active_assignments = RoomAssignment.objects.filter(
        booking_room__booking=booking,
        released_at__isnull=True,
    )
    assigned_room_ids = list(active_assignments.values_list("physical_room_id", flat=True))
    active_assignments.update(released_at=timezone.now())
    booking.status = Booking.Status.CANCELED
    booking.hold_expires_at = None
    booking.save(update_fields=["status", "hold_expires_at", "updated_at"])

    # A confirmed reservation must never make a physical room operationally
    # occupied. Repair legacy/stale states while releasing its assignment, but
    # never vacate a room that is genuinely occupied by another checked-in stay.
    checked_in_room_ids = RoomAssignment.objects.filter(
        physical_room_id__in=assigned_room_ids,
        released_at__isnull=True,
        booking_room__booking__status=Booking.Status.CHECKED_IN,
    ).values_list("physical_room_id", flat=True)
    PhysicalRoom.objects.filter(
        id__in=assigned_room_ids,
        status=PhysicalRoom.Status.OCCUPIED,
    ).exclude(id__in=checked_in_room_ids).update(status=PhysicalRoom.Status.VACANT)
    return booking


def auto_cancel_no_show_reservations(as_of=None):
    """Cancel confirmed reservations whose arrival date has already passed.

    This deliberately reuses the normal cancellation bookkeeping (inventory and
    assignment release), but does not create physical-room action history: a
    no-show never performed a room action such as check-in or check-out.
    """
    as_of = as_of or timezone.localdate()
    booking_ids = list(
        Booking.objects.filter(
            status=Booking.Status.CONFIRMED,
            check_in__lt=as_of,
        ).order_by("check_in", "id").values_list("id", flat=True)
    )
    canceled = 0
    for booking_id in booking_ids:
        booking = Booking.objects.filter(pk=booking_id).first()
        if not booking:
            continue
        try:
            cancel_booking(booking)
        except ValidationError:
            # The status may have changed after the candidate list was read.
            continue
        canceled += 1
    return canceled


@transaction.atomic
def expire_pending_bookings():
    expired = 0
    queryset = Booking.objects.select_for_update().filter(status=Booking.Status.PENDING_PAYMENT, hold_expires_at__lt=timezone.now())
    for booking in queryset:
        _move_inventory(booking, "held_rooms")
        booking.status = Booking.Status.EXPIRED
        booking.save(update_fields=["status", "updated_at"])
        expired += 1
    return expired


@transaction.atomic
def deprovision_hotel(core_business_id):
    hotel = Hotel.objects.select_for_update().filter(core_business_id=core_business_id).first()
    if not hotel:
        return None
    pending = list(Booking.objects.select_for_update().filter(hotel=hotel, status=Booking.Status.PENDING_PAYMENT))
    for booking in pending:
        _move_inventory(booking, "held_rooms")
        booking.status = Booking.Status.EXPIRED
        booking.hold_expires_at = None
        booking.save(update_fields=["status", "hold_expires_at", "updated_at"])
    hotel.is_active = False
    hotel.room_types.update(booking_enabled=False)
    hotel.save(update_fields=["is_active"])
    return hotel
