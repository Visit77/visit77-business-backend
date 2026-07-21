from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

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
    Payment,
    PhysicalRoom,
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


def inventory_window_dates(start_date=None, days=None):
    """Return the rolling inventory dates this service keeps ready for booking."""
    start_date = start_date or timezone.localdate()
    days = settings.BOOKING_INVENTORY_WINDOW_DAYS if days is None else days
    return [start_date + timedelta(days=offset) for offset in range(days + 1)]


def active_sellable_room_count(room_type):
    return room_type.physical_rooms.filter(is_active=True).exclude(status=PhysicalRoom.Status.OUT_OF_SERVICE).count()


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
        DailyInventory(room_type=room_type, stay_date=day, total_rooms=total_rooms)
        for day in missing_dates
    ], ignore_conflicts=True)

    updated = 0
    for row in existing.values():
        committed_rooms = row.held_rooms + row.reserved_rooms
        safe_total_rooms = max(total_rooms, committed_rooms)
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
        if not snapshot.get("allow_guest_smoking_preference", False):
            raise ValidationError({"preferences": f"Smoking preference is not enabled for {room_type.name}."})
        if smoking_type == "smoking" and not snapshot.get("supports_smoking", False):
            raise ValidationError({"preferences": f"{room_type.name} does not support smoking rooms."})
        if smoking_type == "non_smoking" and not snapshot.get("supports_non_smoking", True):
            raise ValidationError({"preferences": f"{room_type.name} does not support non-smoking rooms."})
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
        for room in PhysicalRoom.objects.filter(room_type=room_type, is_active=True).exclude(status=PhysicalRoom.Status.OUT_OF_SERVICE)
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
    ).order_by("hotel_id", "id"))
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
                "base_currency": room_type.hotel.base_currency,
                "display_currency": display_currency or room_type.hotel.base_currency,
                "currency": room_type.hotel.base_currency,
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
            results[room_type.hotel_id].append({
                "core_room_type_id": room_type.core_room_type_id,
                "name": room_type.name,
                "description": room_type.description,
                "cover_image_url": room_type.cover_image_url,
                "max_adults": room_type.max_adults,
                "max_children": room_type.max_children,
                "available_rooms": available,
                "booking_options": room_type_booking_options(room_type),
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
    rooms = []
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
        preference_snapshot, option_total = resolve_room_preferences(
            room_type,
            requested.get("preferences") or {},
            len(dates),
            quantity,
        )
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
        for day in dates:
            rule = daily_rate_rows.get(day) or periods.get(day)
            unit_price, usd_display_price = _rate_amounts(rule, rate_plan)
            extra_bed_total = rate_plan.extra_bed_base_price * requested.get("extra_beds", 0)
            night_total = unit_price * quantity + extra_bed_total + nightly_option_total
            nights.append({
                "stay_date": day,
                "unit_price": unit_price,
                "usd_display_price": usd_display_price,
                "quantity": quantity,
                "extra_bed_total": extra_bed_total,
                "option_total": nightly_option_total,
                "total": night_total,
            })
            item_total += night_total
        room_total += item_total
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
            "nights": nights,
            "total": item_total,
        })
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
        "rooms": rooms,
        "room_total": room_total,
        "grand_total": room_total,
    }


@transaction.atomic
def create_booking(data, idempotency_key=None):
    hotel = Hotel.objects.get(core_business_id=data["core_business_id"], is_active=True)
    if idempotency_key:
        existing = Booking.objects.filter(hotel=hotel, idempotency_key=idempotency_key).first()
        if existing:
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
        if requested.get("adults", 1) > room_type.max_adults * quantity or requested.get("children", 0) > room_type.max_children * quantity:
            raise ValidationError({"rooms": f"Guest count exceeds {room_type.name} capacity."})
        preference_snapshot, option_total = resolve_room_preferences(
            room_type,
            requested.get("preferences") or {},
            len(dates),
            quantity,
        )
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
        nightly_option_total = (option_total / len(dates)) if dates else Decimal("0")
        for day in dates:
            rule = daily_rate_rows.get(day) or periods.get(day)
            unit_price, _usd_display_price = _rate_amounts(rule, rate_plan)
            extra_bed_total = rate_plan.extra_bed_base_price * requested.get("extra_beds", 0)
            night_total = unit_price * quantity + extra_bed_total + nightly_option_total
            BookingRoomNight.objects.create(
                booking_room=booking_room,
                stay_date=day,
                unit_price=unit_price,
                quantity=quantity,
                extra_bed_total=extra_bed_total,
                option_total=nightly_option_total,
                total=night_total,
            )
            item_total += night_total
        booking_room.total = item_total
        booking_room.save(update_fields=["total"])
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


@transaction.atomic
def record_payment(booking, data):
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if (
        booking.status == Booking.Status.PENDING_PAYMENT
        and booking.hold_expires_at
        and booking.hold_expires_at <= timezone.now()
    ):
        raise ValidationError("The booking payment hold has expired.")
    payment = Payment.objects.create(
        booking=booking,
        provider=data["provider"],
        provider_reference=data.get("provider_reference", ""),
        status=data.get("status", Payment.Status.PAID),
        amount=data["amount"],
        currency=booking.currency,
        invoice_number=_reference("IV"),
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
    return payment


@transaction.atomic
def refund_payment(payment, amount, provider_reference=""):
    payment = Payment.objects.select_for_update().select_related("booking").get(pk=payment.pk)
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
    return payment


@transaction.atomic
def cancel_booking(booking):
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if booking.status not in [Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED]:
        raise ValidationError("This booking cannot be canceled.")
    source = "held_rooms" if booking.status == Booking.Status.PENDING_PAYMENT else "reserved_rooms"
    _move_inventory(booking, source)
    RoomAssignment.objects.filter(
        booking_room__booking=booking,
        released_at__isnull=True,
    ).update(released_at=timezone.now())
    booking.status = Booking.Status.CANCELED
    booking.hold_expires_at = None
    booking.save(update_fields=["status", "hold_expires_at", "updated_at"])
    return booking


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
