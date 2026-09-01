from datetime import timedelta, timezone as datetime_timezone
from decimal import Decimal
import json

from django.db.models import Q
from rest_framework import serializers

from booking.add_on_templates import normalize_configuration_schema
from booking.models import (
    AddOn,
    AddOnTemplate,
    AddOnTemplateRequest,
    Booking,
    BookingAddOn,
    BookingRoom,
    BookingRoomNight,
    DailyInventory,
    DailyRate,
    Guest,
    GuestIdentityDocument,
    Hotel,
    Invoice,
    InvoiceLine,
    MealPlan,
    Payment,
    PhysicalRoom,
    PhysicalRoomActionHistory,
    PhysicalRoomBlock,
    RatePlan,
    RatePeriod,
    RoomAssignment,
    RoomType,
    RoomTypeMealPlan,
)


def validate_request_business_scope(serializer, attrs):
    request = serializer.context.get("request")
    core_business_id = getattr(request, "booking_core_business_id", None) if request else None
    if not core_business_id:
        return
    candidate = attrs.get("hotel") or attrs.get("room_type") or attrs.get("rate_plan")
    if candidate is None and serializer.instance is not None:
        candidate = serializer.instance
    hotel = getattr(candidate, "hotel", None)
    if hotel is None and hasattr(candidate, "room_type"):
        hotel = candidate.room_type.hotel
    if hotel is None and hasattr(candidate, "rate_plan"):
        hotel = candidate.rate_plan.room_type.hotel
    if hotel is not None and hotel.core_business_id != core_business_id:
        raise serializers.ValidationError("This object does not belong to the requested business scope.")


class HotelSerializer(serializers.ModelSerializer):
    direct_booking_package = serializers.CharField(source="package", read_only=True)

    def get_fields(self):
        fields = super().get_fields()
        fields.pop("package", None)
        return fields

    class Meta:
        model = Hotel
        fields = "__all__"
        read_only_fields = [
            "core_business_id", "name", "slug", "address", "phone", "cover_image_url",
            "features", "core_snapshot", "access_snapshot", "synced_at",
        ]

    def validate_base_currency(self, value):
        value = value.upper()
        if value not in {"MMK", "USD"}:
            raise serializers.ValidationError("Supported base currencies are MMK and USD.")
        return value


class PublicHotelSerializer(serializers.ModelSerializer):
    direct_booking_package = serializers.CharField(source="package", read_only=True)

    class Meta:
        model = Hotel
        fields = [
            "core_business_id",
            "name",
            "slug",
            "address",
            "phone",
            "cover_image_url",
            "base_currency",
            "direct_booking_package",
            "check_in_time",
            "check_out_time",
        ]


class AvailabilitySearchQuerySerializer(serializers.Serializer):
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    adults = serializers.IntegerField(min_value=1, max_value=100, default=1)
    children = serializers.IntegerField(min_value=0, max_value=100, default=0)
    guest_market = serializers.ChoiceField(
        choices=[RatePlan.GuestMarket.LOCAL, RatePlan.GuestMarket.FOREIGN],
        default=RatePlan.GuestMarket.LOCAL,
    )
    display_currency = serializers.ChoiceField(choices=["MMK", "USD"], required=False)
    q = serializers.CharField(required=False, allow_blank=True, max_length=255, trim_whitespace=True)
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=50, default=20)

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError({"check_out": "Must be after check_in."})
        return attrs


class PublicOTARoomTypeCatalogQuerySerializer(serializers.Serializer):
    guest_market = serializers.ChoiceField(
        choices=[RatePlan.GuestMarket.LOCAL, RatePlan.GuestMarket.FOREIGN],
        required=False,
    )
    display_currency = serializers.ChoiceField(choices=["MMK", "USD"], required=False)


class OTARoomSaleStatusSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["open", "close"])
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)


class OTARoomTimelineQuerySerializer(serializers.Serializer):
    timeline_status = serializers.ChoiceField(
        choices=["all", "active_today", "upcoming", "past"],
        default="all",
    )


class PublicOTARoomTypeCatalogSerializer(serializers.ModelSerializer):
    room_type_id = serializers.IntegerField(source="id", read_only=True)
    photos = serializers.SerializerMethodField()
    amenities = serializers.SerializerMethodField()
    facilities = serializers.SerializerMethodField()
    policies = serializers.SerializerMethodField()
    bed_type = serializers.SerializerMethodField()
    beds = serializers.SerializerMethodField()
    room_standard = serializers.SerializerMethodField()
    room_build_type = serializers.SerializerMethodField()
    room_view = serializers.SerializerMethodField()
    room_area = serializers.SerializerMethodField()
    room_area_from = serializers.SerializerMethodField()
    room_area_to = serializers.SerializerMethodField()
    area_unit = serializers.SerializerMethodField()
    size_sqft = serializers.SerializerMethodField()
    default_prices = serializers.SerializerMethodField()
    default_price = serializers.SerializerMethodField()
    rate_plans = serializers.SerializerMethodField()
    breakfast = serializers.SerializerMethodField()
    meal_plans = serializers.SerializerMethodField()
    ota_enabled_room_count = serializers.IntegerField(read_only=True)
    ota_open_room_count = serializers.IntegerField(read_only=True, default=0)
    ota_enabled = serializers.SerializerMethodField()
    ota_available = serializers.SerializerMethodField()
    availability_calculated = serializers.SerializerMethodField()
    hotel_cancellation_policy = serializers.SerializerMethodField()
    room_cancellation_policy = serializers.SerializerMethodField()
    cancellation_policy = serializers.SerializerMethodField()

    def _snapshot_value(self, obj, key, default=None):
        return (obj.core_snapshot or {}).get(key, default)

    def get_photos(self, obj):
        return self._snapshot_value(obj, "photos", []) or []

    def get_amenities(self, obj):
        return self._snapshot_value(obj, "amenities", []) or []

    def get_facilities(self, obj):
        return self._snapshot_value(obj, "facilities", []) or []

    def get_policies(self, obj):
        return self._snapshot_value(obj, "policies", []) or []

    def get_hotel_cancellation_policy(self, obj):
        policy = (obj.hotel.core_snapshot or {}).get("hotel_cancellation_policy") or self._snapshot_value(
            obj, "hotel_cancellation_policy"
        )
        if policy:
            return policy
        snapshot = obj.core_snapshot or {}
        if not snapshot.get("room_cancellation_policy"):
            plans = self._plans(obj)
            default_plan = next((plan for plan in plans if plan.is_default), None)
            default_plan = default_plan or next(iter(plans), None)
            return default_plan.cancellation_policy if default_plan else None
        return None

    def get_room_cancellation_policy(self, obj):
        snapshot = obj.core_snapshot or {}
        return snapshot.get("room_cancellation_policy")

    def get_cancellation_policy(self, obj):
        snapshot = obj.core_snapshot or {}
        policy = (
            snapshot.get("effective_cancellation_policy")
            or snapshot.get("cancellation_policy")
            or self.get_hotel_cancellation_policy(obj)
        )
        if policy:
            return policy
        plans = self._plans(obj)
        default_plan = next((plan for plan in plans if plan.is_default), None)
        default_plan = default_plan or next(iter(plans), None)
        return default_plan.cancellation_policy if default_plan else None

    def get_bed_type(self, obj):
        return self._snapshot_value(obj, "bed_type")

    def get_beds(self, obj):
        return self._snapshot_value(obj, "beds", []) or []

    def get_room_standard(self, obj):
        return self._snapshot_value(obj, "room_standard")

    def get_room_build_type(self, obj):
        return self._snapshot_value(obj, "room_build_type")

    def get_room_view(self, obj):
        return self._snapshot_value(obj, "room_view")

    def get_room_area(self, obj):
        return self._snapshot_value(obj, "room_area")

    def get_room_area_from(self, obj):
        return self._snapshot_value(obj, "room_area_from")

    def get_room_area_to(self, obj):
        return self._snapshot_value(obj, "room_area_to")

    def get_area_unit(self, obj):
        return self._snapshot_value(obj, "area_unit")

    def get_size_sqft(self, obj):
        return self._snapshot_value(obj, "size_sqft")

    def get_default_prices(self, obj):
        snapshot = obj.core_snapshot or {}
        return {
            "local": {
                "base_price": snapshot.get("local_base_price"),
                "base_currency": snapshot.get("local_base_currency") or obj.hotel.base_currency,
                "usd_display_price": snapshot.get("local_usd_display_price"),
            },
            "foreign": {
                "base_price": snapshot.get("foreign_base_price"),
                "base_currency": snapshot.get("foreign_base_currency") or obj.hotel.base_currency,
                "usd_display_price": snapshot.get("foreign_usd_display_price"),
            },
        }

    def _plans(self, obj):
        plans = getattr(obj, "ota_catalog_rate_plans", None)
        if plans is None:
            plans = list(obj.rate_plans.filter(is_active=True).order_by("guest_market", "code", "id"))
        guest_market = self.context.get("guest_market")
        if guest_market:
            plans = [
                plan for plan in plans
                if plan.guest_market in [guest_market, RatePlan.GuestMarket.ALL]
            ]
        return plans

    def _plan_payload(self, obj, plan):
        display_currency = self.context.get("display_currency") or obj.hotel.base_currency
        display_price = (
            plan.usd_display_price
            if display_currency == "USD" and plan.usd_display_price is not None
            else plan.base_price
        )
        extra_bed_display_price = (
            plan.extra_bed_usd_display_price
            if display_currency == "USD" and plan.extra_bed_usd_display_price is not None
            else plan.extra_bed_base_price
        )
        return {
            "id": plan.id,
            "code": plan.code,
            "name": plan.name,
            "guest_market": plan.guest_market,
            "is_default": plan.is_default,
            "base_price": plan.base_price,
            "base_currency": obj.hotel.base_currency,
            "usd_display_price": plan.usd_display_price,
            "display_price": display_price,
            "display_currency": display_currency,
            "extra_bed_base_price": plan.extra_bed_base_price,
            "extra_bed_usd_display_price": plan.extra_bed_usd_display_price,
            "extra_bed_display_price": extra_bed_display_price,
            "breakfast_included": plan.breakfast_included,
            "refundable": plan.refundable,
            "cancellation_policy": plan.cancellation_policy,
        }

    def get_rate_plans(self, obj):
        return [self._plan_payload(obj, plan) for plan in self._plans(obj)]

    def get_default_price(self, obj):
        plans = self._plans(obj)
        if not plans:
            return None
        plan = next((item for item in plans if item.is_default), plans[0])
        payload = self._plan_payload(obj, plan)
        return {
            "rate_plan_id": payload["id"],
            "guest_market": payload["guest_market"],
            "base_price": payload["base_price"],
            "base_currency": payload["base_currency"],
            "usd_display_price": payload["usd_display_price"],
            "display_price": payload["display_price"],
            "display_currency": payload["display_currency"],
        }

    def get_breakfast(self, obj):
        return RoomTypeSerializer(context=self.context).get_breakfast(obj)

    def get_meal_plans(self, obj):
        links = obj.meal_plan_links.select_related("meal_plan").filter(meal_plan__core_active=True)
        return RoomTypeMealPlanSerializer(links, many=True).data

    def get_availability_calculated(self, obj):
        return False

    def get_ota_available(self, obj):
        return (
            obj.hotel.package in [Hotel.Package.OTA, Hotel.Package.OTA_PMS]
            and int(getattr(obj, "ota_open_room_count", 0) or 0) > 0
        )

    def get_ota_enabled(self, obj):
        return (
            obj.hotel.package in [Hotel.Package.OTA, Hotel.Package.OTA_PMS]
            and int(getattr(obj, "ota_enabled_room_count", 0) or 0) > 0
        )

    class Meta:
        model = RoomType
        fields = [
            "room_type_id", "core_room_type_id", "name", "description", "cover_image_url",
            "photos", "amenities", "facilities", "policies", "bed_type", "beds",
            "room_standard", "room_build_type", "room_view", "room_area", "room_area_from",
            "room_area_to", "area_unit", "size_sqft", "max_adults", "max_children",
            "max_occupancy", "default_prices", "default_price", "rate_plans", "breakfast",
            "meal_plans", "ota_enabled_room_count", "ota_open_room_count", "ota_enabled", "ota_available",
            "availability_calculated",
            "hotel_cancellation_policy", "room_cancellation_policy", "cancellation_policy",
        ]


class RoomBoardQuerySerializer(serializers.Serializer):
    core_business_id = serializers.IntegerField(min_value=1, required=False)
    date = serializers.DateField(required=False)
    building_id = serializers.IntegerField(min_value=1, required=False)
    floor_id = serializers.IntegerField(min_value=1, required=False)
    building = serializers.CharField(required=False, allow_blank=True, max_length=120)
    floor = serializers.CharField(required=False, allow_blank=True, max_length=50)


class RoomTypeSerializer(serializers.ModelSerializer):
    core_business_id = serializers.IntegerField(source="hotel.core_business_id", read_only=True)
    meal_plans = serializers.SerializerMethodField()
    breakfast = serializers.SerializerMethodField()
    rooms = serializers.SerializerMethodField()
    total_room_count = serializers.SerializerMethodField()
    ota_enabled_room_count = serializers.SerializerMethodField()

    def get_meal_plans(self, obj):
        links = obj.meal_plan_links.select_related("meal_plan").all()
        return RoomTypeMealPlanSerializer(links, many=True).data

    def get_breakfast(self, obj):
        selectable = obj.breakfast_plan_type in {
            RoomType.BreakfastPlanType.HOTEL_DEFAULT_PRICE,
            RoomType.BreakfastPlanType.CUSTOM_PRICE,
        }
        plan = obj.hotel.meal_plans.filter(
            is_default_for_room_type_breakfast=True,
            core_active=True,
        ).first() if selectable else None
        price = None
        if obj.breakfast_plan_type == RoomType.BreakfastPlanType.HOTEL_DEFAULT_PRICE and plan:
            price = {
                "local_base_price": plan.local_base_price,
                "local_usd_display_price": plan.local_usd_display_price,
                "foreign_base_price": plan.foreign_base_price,
                "foreign_usd_display_price": plan.foreign_usd_display_price,
            }
        elif obj.breakfast_plan_type == RoomType.BreakfastPlanType.CUSTOM_PRICE:
            price = {
                "local_base_price": obj.breakfast_custom_local_base_price,
                "local_usd_display_price": obj.breakfast_custom_local_usd_display_price,
                "foreign_base_price": obj.breakfast_custom_foreign_base_price,
                "foreign_usd_display_price": obj.breakfast_custom_foreign_usd_display_price,
            }
        return {
            "type": obj.breakfast_plan_type,
            "included": obj.breakfast_plan_type == RoomType.BreakfastPlanType.INCLUDED_IN_ROOM_PRICE,
            "selectable": selectable,
            "meal_plan": MealPlanSerializer(plan).data if plan else None,
            "price": price,
        }

    @staticmethod
    def _rooms(obj):
        prefetched = getattr(obj, "room_type_room_list", None)
        if prefetched is not None:
            return prefetched
        return list(obj.physical_rooms.filter(is_active=True).order_by(
            "building", "floor", "room_number", "id",
        ))

    def get_rooms(self, obj):
        room_type_snapshot = obj.core_snapshot or {}
        room_list = []
        for room in self._rooms(obj):
            room_snapshot = room.core_snapshot or {}
            room_views = room_snapshot.get("room_views") or []
            beds = room_snapshot.get("beds") or []
            room_view = room_snapshot.get("room_view") or (room_views[0] if room_views else None)
            room_view = room_view or room_type_snapshot.get("room_view")
            bed_type = room_snapshot.get("bed_type")
            if not bed_type and beds:
                bed_type = beds[0].get("bed_type") or beds[0]
            bed_type = bed_type or room_type_snapshot.get("bed_type")
            room_area = room_snapshot.get("room_area")
            if room_area is None:
                room_area = room_snapshot.get("size_sqft")
            if room_area is None:
                room_area = room_type_snapshot.get("room_area") or room_type_snapshot.get("size_sqft")
            area_unit = room_snapshot.get("area_unit") or room_type_snapshot.get("area_unit")
            room_list.append({
                "physical_room_id": room.id,
                "core_physical_room_id": room.core_physical_room_id,
                "room_number": room.room_number,
                "building_id": room.core_building_id,
                "building": room.building,
                "floor_id": room.core_floor_id,
                "floor": room.floor,
                "operational_status": room.status,
                "is_active": room.is_active,
                "ota_enabled": room.ota_enabled,
                "is_ota_selected": room.ota_enabled,
                "ota_sale_open": room.ota_sale_open,
                "ota_sale_status": (
                    "not_selected" if not room.ota_enabled
                    else "open" if room.ota_sale_open
                    else "closed"
                ),
                "active_ota_bookings": getattr(room, "active_ota_bookings", 0),
                "room_standard": room_snapshot.get("room_standard") or room_type_snapshot.get("room_standard"),
                "bed_type": bed_type,
                "room_view": room_view,
                "room_area": room_area,
                "area_unit": area_unit,
                "size_sqft": room_area if area_unit == "sqft" else room_snapshot.get("size_sqft"),
                "room_area_text": f"{room_area} {area_unit}" if room_area is not None and area_unit else None,
            })
        return room_list

    def get_total_room_count(self, obj):
        return len(self._rooms(obj))

    def get_ota_enabled_room_count(self, obj):
        return sum(1 for room in self._rooms(obj) if room.ota_enabled)

    class Meta:
        model = RoomType
        fields = "__all__"
        read_only_fields = ["hotel", "core_room_type_id", "name", "description", "cover_image_url", "max_adults", "max_children", "max_occupancy", "core_active", "core_snapshot", "synced_at"]


class MealPlanSerializer(serializers.ModelSerializer):
    core_business_id = serializers.IntegerField(source="hotel.core_business_id", read_only=True)
    includes_breakfast = serializers.BooleanField(read_only=True)

    class Meta:
        model = MealPlan
        fields = "__all__"
        read_only_fields = [
            "hotel", "core_meal_plan_id", "name", "description", "plan_type",
            "package_pricing_mode", "components", "included_meals", "meal_windows",
            "availability", "local_base_price", "local_usd_display_price",
            "foreign_base_price", "foreign_usd_display_price", "core_active",
            "core_snapshot", "synced_at",
        ]


class RoomTypeMealPlanSerializer(serializers.ModelSerializer):
    meal_plan = MealPlanSerializer(read_only=True)
    effective_local_base_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    effective_local_usd_display_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, allow_null=True)
    effective_foreign_base_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    effective_foreign_usd_display_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, allow_null=True)

    class Meta:
        model = RoomTypeMealPlan
        fields = "__all__"
        read_only_fields = [
            "room_type", "meal_plan", "is_included", "is_default", "is_guest_selectable",
            "use_hotel_default_price", "pricing_mode", "local_base_price", "local_usd_display_price",
            "foreign_base_price", "foreign_usd_display_price", "core_snapshot", "synced_at",
        ]


class PhysicalRoomSerializer(serializers.ModelSerializer):
    hotel_id = serializers.IntegerField(source='hotel.id')
    room_type_id = serializers.IntegerField(source='room_type.id')
    class Meta:
        model = PhysicalRoom
        fields = "__all__"
        read_only_fields = ["hotel", "room_type", "core_physical_room_id", "room_number", "floor", "building", "is_active", "ota_enabled", "ota_sale_open"]

    def validate(self, attrs):
        hotel = attrs.get("hotel", getattr(self.instance, "hotel", None))
        room_type = attrs.get("room_type", getattr(self.instance, "room_type", None))
        if hotel and room_type and room_type.hotel_id != hotel.id:
            raise serializers.ValidationError("Room type must belong to the selected hotel.")
        new_status = attrs.get("status")
        if self.instance and new_status and new_status != self.instance.status:
            if new_status == PhysicalRoom.Status.OUT_OF_SERVICE and self.instance.status not in {
                PhysicalRoom.Status.VACANT, PhysicalRoom.Status.CLEANING,
            }:
                raise serializers.ValidationError({
                    "status": "A room can only be marked out of service while vacant or cleaning."
                })
            if new_status == PhysicalRoom.Status.OUT_OF_SERVICE and RoomAssignment.objects.filter(
                physical_room=self.instance,
                released_at__isnull=True,
                booking_room__booking__status__in=[Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN],
            ).exists():
                raise serializers.ValidationError({
                    "status": "A room with an active reservation or stay cannot be marked out of service."
                })
        return attrs


class OTARoomSelectionUpdateSerializer(serializers.Serializer):
    selected_room_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, default=list,
    )
    deselected_room_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, default=list,
    )

    def validate(self, attrs):
        selected = attrs["selected_room_ids"]
        deselected = attrs["deselected_room_ids"]
        if len(selected) != len(set(selected)):
            raise serializers.ValidationError({"selected_room_ids": "Duplicate room IDs are not allowed."})
        if len(deselected) != len(set(deselected)):
            raise serializers.ValidationError({"deselected_room_ids": "Duplicate room IDs are not allowed."})
        overlap = sorted(set(selected).intersection(deselected))
        if overlap:
            raise serializers.ValidationError({
                "room_ids": f"The same room cannot be selected and deselected: {overlap}."
            })
        if not selected and not deselected:
            raise serializers.ValidationError("At least one selected or deselected room ID is required.")
        return attrs


class PhysicalRoomBlockSerializer(serializers.ModelSerializer):
    class Meta:
        model = PhysicalRoomBlock
        fields = "__all__"
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, attrs):
        room = attrs.get("physical_room", getattr(self.instance, "physical_room", None))
        start_date = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end_date = attrs.get("end_date", getattr(self.instance, "end_date", None))
        is_active = attrs.get("is_active", getattr(self.instance, "is_active", True))
        request = self.context.get("request")
        core_business_id = getattr(request, "booking_core_business_id", None) if request else None
        if core_business_id and room and room.hotel.core_business_id != core_business_id:
            raise serializers.ValidationError({"physical_room": "Room does not belong to this business."})
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError({"end_date": "Must be on or after start_date."})
        if not (room and start_date and end_date and is_active):
            return attrs
        booking_conflicts = RoomAssignment.objects.filter(
            physical_room=room,
            released_at__isnull=True,
            booking_room__booking__status__in=[Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN],
            booking_room__booking__check_in__lte=end_date,
            booking_room__booking__check_out__gt=start_date,
        ).select_related("booking_room__booking").order_by(
            "booking_room__booking__check_in", "booking_room__booking__reference",
        )
        conflict_details = []
        conflict_bookings = []
        seen_booking_ids = set()
        for assignment in booking_conflicts:
            booking = assignment.booking_room.booking
            if booking.id in seen_booking_ids:
                continue
            seen_booking_ids.add(booking.id)
            display_status = (
                "occupied" if booking.status == Booking.Status.CHECKED_IN else "reserved"
            )
            overlap_start = max(start_date, booking.check_in)
            overlap_end = min(end_date, booking.check_out - timedelta(days=1))
            conflict_details.append(
                f"Booking {booking.reference} ({display_status}) is from "
                f"{booking.check_in} to {booking.check_out} and conflicts from "
                f"{overlap_start} to {overlap_end}."
            )
            conflict_bookings.append({
                "booking_id": str(booking.id),
                "reference": booking.reference,
                "status": display_status,
                "booking_status": booking.status,
                "contact_name": booking.contact_name,
                "check_in": str(booking.check_in),
                "check_out": str(booking.check_out),
                "conflict_start_date": str(overlap_start),
                "conflict_end_date": str(overlap_end),
            })
        if conflict_details:
            raise serializers.ValidationError({
                "date_range": " ".join(conflict_details),
                "conflict_bookings": conflict_bookings,
            })
        overlapping_blocks = PhysicalRoomBlock.objects.filter(
            physical_room=room,
            is_active=True,
            start_date__lte=end_date,
            end_date__gte=start_date,
        )
        if self.instance:
            overlapping_blocks = overlapping_blocks.exclude(pk=self.instance.pk)
        if overlapping_blocks.exists():
            raise serializers.ValidationError({"date_range": "This room already has an overlapping block."})
        return attrs


class PhysicalRoomActionHistorySerializer(serializers.ModelSerializer):
    action = serializers.SerializerMethodField()
    raw_action = serializers.CharField(source="action", read_only=True)
    created_at = serializers.DateTimeField(
        read_only=True,
        default_timezone=datetime_timezone.utc,
    )
    performed_at = serializers.DateTimeField(
        source="created_at",
        read_only=True,
        default_timezone=datetime_timezone.utc,
    )
    action_label = serializers.SerializerMethodField()
    actor_type_label = serializers.CharField(source="get_actor_type_display", read_only=True)
    actor = serializers.SerializerMethodField()
    guest = serializers.SerializerMethodField()
    booking_reference = serializers.CharField(source="booking.reference", read_only=True, allow_null=True)
    guest_name = serializers.CharField(source="booking.contact_name", read_only=True, allow_null=True)
    invoice_numbers = serializers.SerializerMethodField()

    class Meta:
        model = PhysicalRoomActionHistory
        fields = [
            "id", "physical_room", "action", "raw_action", "action_label",
            "created_at", "performed_at", "actor", "guest",
            "old_status", "new_status", "note", "actor_type", "actor_type_label",
            "actor_core_user_id", "booking", "booking_reference", "guest_name",
            "block", "invoice_numbers", "metadata",
        ]
        read_only_fields = fields

    ACTION_PRESENTATION = {
        PhysicalRoomActionHistory.Action.CLEANING_COMPLETED: ("cleaned", "Cleaned"),
        PhysicalRoomActionHistory.Action.VACANT: ("cleaned", "Cleaned"),
        PhysicalRoomActionHistory.Action.OUT_OF_SERVICE_STARTED: ("out_of_service", "Out of Service"),
        PhysicalRoomActionHistory.Action.OUT_OF_SERVICE_ENDED: ("oos_repaired", "OOS Repaired"),
    }

    def get_action(self, obj):
        return self.ACTION_PRESENTATION.get(obj.action, (obj.action, obj.get_action_display()))[0]

    def get_action_label(self, obj):
        return self.ACTION_PRESENTATION.get(obj.action, (obj.action, obj.get_action_display()))[1]

    def get_actor(self, obj):
        return {
            "type": obj.actor_type,
            "type_label": obj.get_actor_type_display(),
            "core_user_id": obj.actor_core_user_id,
            "name": obj.metadata.get("actor_name") or None,
            "email": obj.metadata.get("actor_email") or None,
        }

    def get_guest(self, obj):
        if not obj.booking_id:
            return None
        return {
            "name": obj.booking.contact_name,
            "phone": obj.booking.contact_phone,
        }

    def get_invoice_numbers(self, obj):
        if not obj.booking_id:
            return []
        return [payment.invoice_number for payment in obj.booking.payments.all()]


class RatePlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = RatePlan
        fields = "__all__"
        read_only_fields = ["core_rate_plan_id", "source", "is_default"]
        extra_kwargs = {
            "base_price": {"min_value": Decimal("0")},
            "usd_display_price": {"min_value": Decimal("0"), "required": False, "allow_null": True},
            "extra_bed_base_price": {"min_value": Decimal("0")},
            "extra_bed_usd_display_price": {"min_value": Decimal("0"), "required": False, "allow_null": True},
            "default_price": {"min_value": Decimal("0"), "required": False},
            "extra_bed_price": {"min_value": Decimal("0")},
        }

    def validate(self, attrs):
        validate_request_business_scope(self, attrs)
        if self.instance and self.instance.source == RatePlan.Source.CORE:
            raise serializers.ValidationError("Core-generated default RatePlans are read-only.")
        if self.instance and "room_type" in attrs and attrs["room_type"] != self.instance.room_type:
            raise serializers.ValidationError({"room_type": "A RatePlan cannot be moved to another room type."})
        if "base_price" not in attrs and "default_price" in attrs:
            attrs["base_price"] = attrs["default_price"]
        if "default_price" not in attrs and "base_price" in attrs:
            attrs["default_price"] = attrs["base_price"]
        if self.instance is None and "base_price" not in attrs:
            raise serializers.ValidationError({"base_price": "This field is required."})
        if "extra_bed_base_price" not in attrs and "extra_bed_price" in attrs:
            attrs["extra_bed_base_price"] = attrs["extra_bed_price"]
        if "extra_bed_price" not in attrs and "extra_bed_base_price" in attrs:
            attrs["extra_bed_price"] = attrs["extra_bed_base_price"]
        room_type = attrs.get("room_type", getattr(self.instance, "room_type", None))
        if room_type:
            attrs["currency"] = room_type.hotel.base_currency
        return attrs


class CoreEventSerializer(serializers.Serializer):
    event_id = serializers.UUIDField()
    event_type = serializers.ChoiceField(choices=[
        "direct_booking.activated",
        "direct_booking.reconcile",
        "direct_booking.catalog_changed",
        "direct_booking.revoked",
        "direct_booking.expired",
    ])
    business_id = serializers.IntegerField(min_value=1)
    payload = serializers.JSONField(required=False, default=dict)


class DailyInventorySerializer(serializers.ModelSerializer):
    available_rooms = serializers.IntegerField(read_only=True)

    class Meta:
        model = DailyInventory
        fields = "__all__"
        read_only_fields = ["held_rooms", "reserved_rooms"]

    def validate(self, attrs):
        validate_request_business_scope(self, attrs)
        return attrs


class DailyInventoryBulkUpsertSerializer(serializers.Serializer):
    room_type_id = serializers.PrimaryKeyRelatedField(
        source="room_type",
        queryset=RoomType.objects.select_related("hotel"),
    )
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    total_rooms = serializers.IntegerField(min_value=0, max_value=10000)
    stop_sell = serializers.BooleanField(default=False)

    def validate(self, attrs):
        validate_request_business_scope(self, attrs)
        if attrs["end_date"] < attrs["start_date"]:
            raise serializers.ValidationError({"end_date": "Must be on or after start_date."})
        if (attrs["end_date"] - attrs["start_date"]).days + 1 > 731:
            raise serializers.ValidationError({"end_date": "A single update cannot exceed 731 days."})
        return attrs


class DailyRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyRate
        fields = "__all__"
        extra_kwargs = {
            "base_price": {"min_value": Decimal("0")},
            "usd_display_price": {"min_value": Decimal("0"), "required": False, "allow_null": True},
            "price": {"min_value": Decimal("0"), "required": False},
        }

    def validate(self, attrs):
        validate_request_business_scope(self, attrs)
        submitted = set(getattr(self, "initial_data", {}).keys())
        if "base_price" not in attrs and "price" in attrs:
            attrs["base_price"] = attrs["price"]
        if "price" not in attrs and "base_price" in attrs:
            attrs["price"] = attrs["base_price"]
        if self.instance is None and not submitted.intersection({"base_price", "price"}):
            raise serializers.ValidationError({"price": "This field is required. Use base_price for the new API."})
        return attrs


class DailyRateBulkUpsertSerializer(serializers.Serializer):
    rate_plan_id = serializers.PrimaryKeyRelatedField(
        source="rate_plan",
        queryset=RatePlan.objects.all(),
    )
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    base_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False)
    usd_display_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True)
    price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False)
    min_stay = serializers.IntegerField(min_value=1, max_value=365, default=1)
    closed_to_arrival = serializers.BooleanField(default=False)
    closed_to_departure = serializers.BooleanField(default=False)

    def validate(self, attrs):
        validate_request_business_scope(self, attrs)
        if "base_price" not in attrs and "price" in attrs:
            attrs["base_price"] = attrs["price"]
        if "price" not in attrs and "base_price" in attrs:
            attrs["price"] = attrs["base_price"]
        if "base_price" not in attrs:
            raise serializers.ValidationError({"base_price": "This field is required."})
        if attrs["end_date"] < attrs["start_date"]:
            raise serializers.ValidationError({"end_date": "Must be on or after start_date."})
        number_of_days = (attrs["end_date"] - attrs["start_date"]).days + 1
        if number_of_days > 731:
            raise serializers.ValidationError({"end_date": "A single update cannot exceed 731 days."})
        return attrs


class RatePeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = RatePeriod
        fields = "__all__"
        read_only_fields = ["created_at", "updated_at"]
        extra_kwargs = {
            "base_price": {"min_value": Decimal("0")},
            "usd_display_price": {"min_value": Decimal("0"), "required": False, "allow_null": True},
            "price": {"min_value": Decimal("0"), "required": False},
        }

    def validate(self, attrs):
        validate_request_business_scope(self, attrs)
        submitted = set(getattr(self, "initial_data", {}).keys())
        if "base_price" not in attrs and "price" in attrs:
            attrs["base_price"] = attrs["price"]
        if "price" not in attrs and "base_price" in attrs:
            attrs["price"] = attrs["base_price"]
        if self.instance is None and not submitted.intersection({"base_price", "price"}):
            raise serializers.ValidationError({"base_price": "This field is required."})
        rate_plan = attrs.get("rate_plan", getattr(self.instance, "rate_plan", None))
        start_date = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end_date = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if end_date and end_date < start_date:
            raise serializers.ValidationError({"end_date": "Must be on or after start_date."})

        if attrs.get("is_active", getattr(self.instance, "is_active", True)):
            queryset = RatePeriod.objects.filter(rate_plan=rate_plan, is_active=True)
            if self.instance:
                queryset = queryset.exclude(pk=self.instance.pk)
            # Existing starts before this period ends, and existing ends after this period starts.
            if end_date:
                queryset = queryset.filter(start_date__lte=end_date)
            queryset = queryset.filter(Q(end_date__isnull=True) | Q(end_date__gte=start_date))
            if queryset.exists():
                raise serializers.ValidationError("An active rate period overlaps this date range.")
        return attrs


class AddOnSerializer(serializers.ModelSerializer):
    class Meta:
        model = AddOn
        fields = "__all__"
        read_only_fields = ["template"]

    def validate(self, attrs):
        validate_request_business_scope(self, attrs)
        service_type = attrs.get("service_type", getattr(self.instance, "service_type", "custom"))
        template = AddOnTemplate.objects.filter(
            code=service_type,
            status=AddOnTemplate.Status.PUBLISHED,
        ).order_by("-version", "-id").first()
        if template is None:
            raise serializers.ValidationError({"service_type": "No published template exists for this service type."})

        pricing_unit = attrs.get("pricing_unit", getattr(self.instance, "pricing_unit", AddOn.PricingUnit.PER_BOOKING))
        if pricing_unit not in template.allowed_pricing_units:
            allowed = ", ".join(template.allowed_pricing_units)
            raise serializers.ValidationError({"pricing_unit": f"Allowed values for this service type: {allowed}."})

        if "configuration_schema" in attrs:
            try:
                attrs["configuration_schema"] = normalize_configuration_schema(attrs["configuration_schema"])
            except ValueError as exc:
                raise serializers.ValidationError({"configuration_schema": str(exc)}) from exc
        elif self.instance is None or "service_type" in attrs:
            attrs["configuration_schema"] = template.configuration_schema
        if self.instance is None or "service_type" in attrs:
            attrs["template"] = template
        return attrs


class AddOnTemplateSerializer(serializers.ModelSerializer):
    code = serializers.SlugField(max_length=80)
    type = serializers.CharField(source="code", read_only=True)

    class Meta:
        model = AddOnTemplate
        fields = "__all__"
        read_only_fields = ["version", "created_by_core_user_id", "published_at", "created_at", "updated_at"]
        validators = []

    def validate_allowed_pricing_units(self, value):
        allowed = {choice for choice, _ in AddOn.PricingUnit.choices}
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError("At least one pricing unit is required.")
        if any(item not in allowed for item in value):
            raise serializers.ValidationError("Contains an unsupported pricing unit.")
        return list(dict.fromkeys(value))

    def validate_configuration_schema(self, value):
        try:
            return normalize_configuration_schema(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class AddOnTemplateRequestSerializer(serializers.ModelSerializer):
    core_business_id = serializers.IntegerField(source="hotel.core_business_id", read_only=True)
    hotel_name = serializers.CharField(source="hotel.name", read_only=True)
    approved_template_data = AddOnTemplateSerializer(source="approved_template", read_only=True)

    class Meta:
        model = AddOnTemplateRequest
        fields = "__all__"
        read_only_fields = [
            "hotel", "status", "requested_by_core_user_id", "reviewed_by_core_user_id",
            "admin_note", "approved_template", "created_at", "updated_at", "reviewed_at",
        ]

    def validate_suggested_pricing_units(self, value):
        allowed = {choice for choice, _ in AddOn.PricingUnit.choices}
        if not isinstance(value, list) or not value or any(item not in allowed for item in value):
            raise serializers.ValidationError("Use one or more supported pricing units.")
        return list(dict.fromkeys(value))

    def validate_suggested_schema(self, value):
        try:
            return normalize_configuration_schema(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class AddOnTemplateApprovalSerializer(serializers.Serializer):
    code = serializers.SlugField(max_length=80)
    name = serializers.CharField(max_length=255, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    allowed_pricing_units = serializers.ListField(child=serializers.ChoiceField(choices=AddOn.PricingUnit.choices), required=False)
    configuration_schema = serializers.JSONField(required=False)
    admin_note = serializers.CharField(required=False, allow_blank=True)

    def validate_configuration_schema(self, value):
        try:
            return normalize_configuration_schema(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class AddOnTemplateRejectionSerializer(serializers.Serializer):
    admin_note = serializers.CharField(allow_blank=False)


class GuestSerializer(serializers.ModelSerializer):
    documents = serializers.SerializerMethodField()

    def get_documents(self, obj):
        return [
            {
                "id": document.id,
                "document_type": document.document_type,
                "document_number": document.document_number,
                "file_url": document.file.url if document.file else None,
                "is_verified": document.is_verified,
                "verified_at": document.verified_at,
                "uploaded_at": document.uploaded_at,
            }
            for document in obj.identity_documents.all()
        ]

    class Meta:
        model = Guest
        exclude = ["booking"]


class BookingRoomNightSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingRoomNight
        exclude = ["booking_room"]


class BookingRoomSerializer(serializers.ModelSerializer):
    nights = BookingRoomNightSerializer(many=True, read_only=True)
    room_type_name = serializers.CharField(source="room_type.name", read_only=True)
    room_type_id = serializers.CharField(source="room_type.id", read_only=True)
    rate_plan_name = serializers.CharField(source="rate_plan.name", read_only=True)

    class Meta:
        model = BookingRoom
        # fields = "__all__"
        exclude = ["room_type"]


class BookingAddOnSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="add_on.name", read_only=True)

    class Meta:
        model = BookingAddOn
        fields = "__all__"


class PaymentSerializer(serializers.ModelSerializer):
    current_invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)

    class Meta:
        model = Payment
        fields = "__all__"


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = ["id", "description", "quantity", "unit_price", "total", "metadata", "created_at"]


class InvoiceSerializer(serializers.ModelSerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)
    receipts = PaymentSerializer(many=True, read_only=True)
    paid_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    balance = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    invoice_details = serializers.SerializerMethodField()

    def get_invoice_details(self, obj):
        booking = obj.booking
        primary_guest = booking.guests.filter(is_primary=True).first()

        def money(value):
            return f"{Decimal(value):.2f}"

        line_totals = {}
        for line in obj.lines.all():
            line_type = (line.metadata or {}).get("line_type", "other")
            line_totals[line_type] = line_totals.get(line_type, Decimal("0")) + line.total
        room_charge_total = line_totals.get("room", Decimal("0")) + line_totals.get("extra_bed", Decimal("0"))
        additional_charge_total = sum(
            (amount for line_type, amount in line_totals.items() if line_type not in {"room", "extra_bed", "service_fee"}),
            Decimal("0"),
        )
        deposit_amount = sum(
            (
                payment.amount - payment.refunded_amount
                for payment in obj.receipts.all()
                if payment.payment_type == Payment.Type.DEPOSIT
                and payment.status in {Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED}
            ),
            Decimal("0"),
        )
        return {
            "hotel": {
                "name": booking.hotel.name,
                "address": booking.hotel.address,
                "phone": booking.hotel.phone,
            },
            "guest_name": primary_guest.name if primary_guest else booking.contact_name,
            "contact_number": (primary_guest.phone if primary_guest and primary_guest.phone else booking.contact_phone),
            "check_in": booking.check_in,
            "check_out": booking.check_out,
            "check_in_time": booking.hotel.check_in_time,
            "check_out_time": booking.hotel.check_out_time,
            "room_charge_total": money(room_charge_total),
            "additional_charge_total": money(additional_charge_total),
            "service_fee_total": money(line_totals.get("service_fee", Decimal("0"))),
            "subtotal": money(obj.subtotal),
            "tax_total": money(obj.tax_total),
            "discount_total": money(obj.discount_total),
            "grand_total": money(obj.total),
            "deposit_amount": money(deposit_amount),
            "amount_due": money(obj.balance),
            "refund_policy": booking.cancellation_policy_snapshot,
        }

    class Meta:
        model = Invoice
        fields = "__all__"


class InvoiceLineCreateSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=255)
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"), default=Decimal("1"))
    unit_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"))
    metadata = serializers.JSONField(required=False, default=dict)


class InvoiceCreateSerializer(serializers.Serializer):
    invoice_type = serializers.ChoiceField(choices=Invoice.Type.choices)
    lines = InvoiceLineCreateSerializer(many=True, allow_empty=False)
    tax_total = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), default=Decimal("0"))
    discount_total = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), default=Decimal("0"))
    note = serializers.CharField(required=False, allow_blank=True, default="")


class BookingSerializer(serializers.ModelSerializer):
    rooms = BookingRoomSerializer(many=True, read_only=True)
    guests = GuestSerializer(many=True, read_only=True)
    add_ons = BookingAddOnSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    invoices = InvoiceSerializer(many=True, read_only=True)
    core_business_id = serializers.IntegerField(source="hotel.core_business_id", read_only=True)
    hotel_name = serializers.CharField(source="hotel.name", read_only=True)
    nights = serializers.IntegerField(read_only=True)
    hotel_id = serializers.IntegerField(source='hotel.id')

    class Meta:
        model = Booking
        # fields = "__all__"
        exclude = ["hotel"]
        


class RequestedRoomPreferenceSerializer(serializers.Serializer):
    preference_standard = serializers.ChoiceField(
        choices=["twin_bed", "large_bed"],
        required=False,
    )
    core_bed_type_id = serializers.IntegerField(min_value=1, required=False)
    core_room_view_id = serializers.IntegerField(min_value=1, required=False)
    core_bath_type_id = serializers.IntegerField(min_value=1, required=False)
    smoking_type = serializers.ChoiceField(choices=["non_smoking", "smoking"], required=False)
    core_custom_option_value_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
    )


class RequestedRoomSerializer(serializers.Serializer):
    core_room_type_id = serializers.IntegerField(min_value=1)
    rate_plan_id = serializers.IntegerField(min_value=1)
    meal_plan_link_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    meal_plan_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    meal_plan_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=True,
    )
    breakfast_selected = serializers.BooleanField(required=False, default=False)
    quantity = serializers.IntegerField(min_value=1, max_value=20)
    adults = serializers.IntegerField(min_value=1, max_value=100, default=1)
    children = serializers.IntegerField(min_value=0, max_value=100, default=0)
    extra_beds = serializers.IntegerField(min_value=0, max_value=20, default=0)
    preferences = RequestedRoomPreferenceSerializer(required=False, default=dict)

    def validate(self, attrs):
        selected_fields = sum(bool(attrs.get(field)) for field in (
            "meal_plan_id", "meal_plan_link_id", "meal_plan_ids",
        ))
        if selected_fields > 1:
            raise serializers.ValidationError({
                "meal_plan_ids": "Send only one of meal_plan_ids, meal_plan_id, or meal_plan_link_id."
            })
        meal_plan_ids = attrs.get("meal_plan_ids") or []
        if len(meal_plan_ids) != len(set(meal_plan_ids)):
            raise serializers.ValidationError({"meal_plan_ids": "Duplicate meal plans are not allowed."})
        return attrs


class RequestedGuestSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    phone = serializers.CharField(max_length=64, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    nationality = serializers.CharField(max_length=100, required=False, allow_blank=True)
    nrc_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    passport_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    identity_type = serializers.CharField(max_length=50, required=False, allow_blank=True)
    identity_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    is_primary = serializers.BooleanField(default=False)


class GuestIdentityDocumentSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    def get_file_url(self, obj):
        return obj.file.url if obj.file else None

    class Meta:
        model = GuestIdentityDocument
        fields = [
            "id", "guest", "document_type", "document_number", "file", "file_url",
            "is_verified", "verified_at", "verified_by_core_user_id", "uploaded_at",
        ]
        read_only_fields = ["id", "guest", "file_url", "is_verified", "verified_at", "verified_by_core_user_id", "uploaded_at"]
        extra_kwargs = {"file": {"write_only": True}}


class GuestIdentityDocumentUploadSerializer(serializers.Serializer):
    guest_id = serializers.IntegerField(min_value=1)
    document_type = serializers.ChoiceField(choices=GuestIdentityDocument.DocumentType.choices)
    document_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    file = serializers.FileField()


class AdminReservationRoomSerializer(RequestedRoomSerializer):
    physical_room_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )


class InitialPaymentSerializer(serializers.Serializer):
    payment_type = serializers.ChoiceField(
        choices=[Payment.Type.DEPOSIT, Payment.Type.FULL_PAYMENT],
        required=False,
    )
    provider = serializers.ChoiceField(choices=Payment.Provider.choices, default=Payment.Provider.CASH)
    provider_reference = serializers.CharField(max_length=255, required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=[Payment.Status.PAID, Payment.Status.PENDING],
        default=Payment.Status.PAID,
    )
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False,
    )
    metadata = serializers.JSONField(required=False, default=dict)

    def validate(self, attrs):
        if "payment_type" not in attrs:
            attrs["payment_type"] = Payment.Type.DEPOSIT if "amount" in attrs else Payment.Type.FULL_PAYMENT
        if attrs["payment_type"] == Payment.Type.DEPOSIT and "amount" not in attrs:
            raise serializers.ValidationError({"amount": "Amount is required for a deposit."})
        if attrs["status"] == Payment.Status.PENDING and "amount" not in attrs:
            raise serializers.ValidationError({"amount": "Amount is required for a pending payment transaction."})
        return attrs


class CheckInGuestUpdateSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1, required=False)
    name = serializers.CharField(max_length=255)
    phone = serializers.CharField(max_length=64, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    nationality = serializers.CharField(max_length=100, required=False, allow_blank=True)
    nrc_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    passport_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    identity_type = serializers.CharField(max_length=50, required=False, allow_blank=True)
    identity_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    is_primary = serializers.BooleanField(required=False)


class CheckInAddOnUpdateSerializer(serializers.Serializer):
    add_on_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=100, default=1)
    configuration = serializers.JSONField(required=False, default=dict)


class CheckInFormUpdateSerializer(serializers.Serializer):
    workflow = serializers.ChoiceField(
        choices=["direct_check_in", "reservation"],
        required=False,
        write_only=True,
    )
    physical_room_id = serializers.IntegerField(min_value=1, required=False)
    rate_plan_id = serializers.IntegerField(min_value=1, required=False)
    check_in = serializers.DateField(required=False)
    check_out = serializers.DateField(required=False)
    contact_name = serializers.CharField(max_length=255, required=False)
    contact_phone = serializers.CharField(max_length=64, required=False)
    contact_email = serializers.EmailField(required=False, allow_blank=True)
    guest_market = serializers.ChoiceField(choices=RatePlan.GuestMarket.choices, required=False)
    special_request = serializers.CharField(required=False, allow_blank=True)
    adults = serializers.IntegerField(min_value=1, required=False)
    children = serializers.IntegerField(min_value=0, required=False)
    extra_beds = serializers.IntegerField(min_value=0, required=False)
    rooms = AdminReservationRoomSerializer(many=True, required=False)
    add_ons = CheckInAddOnUpdateSerializer(many=True, required=False)
    guests = CheckInGuestUpdateSerializer(many=True, required=False)
    payment = InitialPaymentSerializer(required=False, allow_null=True)

    def validate(self, attrs):
        booking = self.context.get("booking")
        check_in = attrs.get("check_in", booking.check_in if booking else None)
        check_out = attrs.get("check_out", booking.check_out if booking else None)
        if check_in and check_out and check_out <= check_in:
            raise serializers.ValidationError({"check_out": "Must be after check_in."})
        if check_in and check_out and (check_out - check_in).days > 90:
            raise serializers.ValidationError({"check_out": "A stay cannot exceed 90 nights."})
        if attrs.get("rooms") and any(key in attrs for key in ["adults", "children", "extra_beds"]):
            raise serializers.ValidationError({
                "rooms": "Send guest counts either inside rooms or as top-level fields, not both."
            })
        if attrs.get("rooms") and any(key in attrs for key in ["physical_room_id", "rate_plan_id"]):
            raise serializers.ValidationError({
                "rooms": "Send room selection either inside rooms or as top-level fields, not both."
            })
        if booking and booking.rooms.count() != 1 and any(
            key in attrs for key in ["physical_room_id", "rate_plan_id", "adults", "children", "extra_beds"]
        ):
            raise serializers.ValidationError({
                "rooms": "Top-level room fields can only be used for a single-room booking."
            })
        return attrs


class CheckInConfirmSerializer(serializers.Serializer):
    verification_confirmed = serializers.BooleanField()
    verification_note = serializers.CharField(required=False, allow_blank=True)


class RequestedAddOnSerializer(serializers.Serializer):
    add_on_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=100, default=1)
    configuration = serializers.JSONField(required=False, default=dict)


class BookingCreateSerializer(serializers.Serializer):
    core_business_id = serializers.IntegerField(min_value=1)
    core_customer_user_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    contact_name = serializers.CharField(max_length=255)
    contact_phone = serializers.CharField(max_length=64)
    contact_email = serializers.EmailField(required=False, allow_blank=True)
    guest_market = serializers.ChoiceField(choices=RatePlan.GuestMarket.choices, default=RatePlan.GuestMarket.LOCAL)
    special_request = serializers.CharField(required=False, allow_blank=True)
    rooms = RequestedRoomSerializer(many=True, allow_empty=False)
    guests = RequestedGuestSerializer(many=True, required=False)
    add_ons = RequestedAddOnSerializer(many=True, required=False)

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError({"check_out": "Must be after check_in."})
        if (attrs["check_out"] - attrs["check_in"]).days > 90:
            raise serializers.ValidationError({"check_out": "A stay cannot exceed 90 nights."})
        return attrs


class AdminReservationCreateSerializer(BookingCreateSerializer):
    source = serializers.ChoiceField(
        choices=[Booking.Source.PHONE, Booking.Source.PMS, Booking.Source.WALK_IN],
        default=Booking.Source.PHONE,
    )
    source_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    rooms = AdminReservationRoomSerializer(many=True, allow_empty=False)
    payment = InitialPaymentSerializer(required=False, allow_null=True)
    # Backward compatibility for clients already sending the old field.
    deposit = InitialPaymentSerializer(required=False, allow_null=True, write_only=True)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs.get("payment") and attrs.get("deposit"):
            raise serializers.ValidationError({"payment": "Send either payment or legacy deposit, not both."})
        legacy_deposit = attrs.pop("deposit", None)
        if legacy_deposit:
            attrs["payment"] = {**legacy_deposit, "payment_type": Payment.Type.DEPOSIT}
        return attrs


class PMSAvailableRoomSearchSerializer(serializers.Serializer):
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    adults = serializers.IntegerField(min_value=1, default=1)
    children = serializers.IntegerField(min_value=0, default=0)
    guest_market = serializers.ChoiceField(
        choices=RatePlan.GuestMarket.choices,
        default=RatePlan.GuestMarket.LOCAL,
    )
    workflow = serializers.ChoiceField(
        choices=[("reserve", "Reserve"), ("check_in", "Immediate check-in")],
        default="reserve",
    )
    selected_room_ids = serializers.CharField(required=False, allow_blank=True, default="")
    current_room_id = serializers.IntegerField(min_value=1, required=False)
    current_rate_plan_id = serializers.IntegerField(min_value=1, required=False)

    @staticmethod
    def _parse_room_ids(raw_value):
        if not raw_value:
            return []
        if isinstance(raw_value, (list, tuple)):
            values = raw_value
        else:
            raw_value = str(raw_value).strip()
            if raw_value.startswith("["):
                try:
                    values = json.loads(raw_value)
                except (TypeError, ValueError):
                    raise serializers.ValidationError("Use comma-separated IDs or a JSON array.")
            else:
                values = [item.strip() for item in raw_value.split(",") if item.strip()]
        try:
            room_ids = [int(item) for item in values]
        except (TypeError, ValueError):
            raise serializers.ValidationError("Every selected room ID must be an integer.")
        if any(room_id <= 0 for room_id in room_ids):
            raise serializers.ValidationError("Every selected room ID must be positive.")
        if len(room_ids) != len(set(room_ids)):
            raise serializers.ValidationError("Duplicate selected room IDs are not allowed.")
        return room_ids

    def validate_selected_room_ids(self, value):
        return self._parse_room_ids(value)

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError({"check_out": "Must be after check_in."})
        if (attrs["check_out"] - attrs["check_in"]).days > 90:
            raise serializers.ValidationError({"check_out": "A stay cannot exceed 90 nights."})
        return attrs


class BookingEstimateSerializer(serializers.Serializer):
    core_business_id = serializers.IntegerField(min_value=1)
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    guest_market = serializers.ChoiceField(choices=RatePlan.GuestMarket.choices, default=RatePlan.GuestMarket.LOCAL)
    rooms = RequestedRoomSerializer(many=True, allow_empty=False)
    add_ons = RequestedAddOnSerializer(many=True, required=False)
    guests = RequestedGuestSerializer(many=True, required=False, default=list)

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError({"check_out": "Must be after check_in."})
        if (attrs["check_out"] - attrs["check_in"]).days > 90:
            raise serializers.ValidationError({"check_out": "A stay cannot exceed 90 nights."})
        return attrs


class WalkInPaymentSerializer(InitialPaymentSerializer):
    pass


class WalkInRoomSerializer(serializers.Serializer):
    physical_room_id = serializers.IntegerField(min_value=1)
    rate_plan_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    meal_plan_link_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    meal_plan_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    meal_plan_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=True,
    )
    breakfast_selected = serializers.BooleanField(required=False, default=False)
    adults = serializers.IntegerField(min_value=1, max_value=100, default=1)
    children = serializers.IntegerField(min_value=0, max_value=100, default=0)
    extra_beds = serializers.IntegerField(min_value=0, max_value=20, default=0)
    preferences = RequestedRoomPreferenceSerializer(required=False, default=dict)

    def validate(self, attrs):
        if sum(bool(attrs.get(field)) for field in (
            "meal_plan_id", "meal_plan_link_id", "meal_plan_ids",
        )) > 1:
            raise serializers.ValidationError({
                "meal_plan_ids": "Send only one of meal_plan_ids, meal_plan_id, or meal_plan_link_id."
            })
        meal_plan_ids = attrs.get("meal_plan_ids") or []
        if len(meal_plan_ids) != len(set(meal_plan_ids)):
            raise serializers.ValidationError({"meal_plan_ids": "Duplicate meal plans are not allowed."})
        return attrs


class WalkInBookingCreateSerializer(serializers.Serializer):
    workflow = serializers.ChoiceField(
        choices=["direct_check_in", "reservation"],
        default="reservation",
        write_only=True,
    )
    physical_room_id = serializers.IntegerField(min_value=1, required=False)
    rate_plan_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    meal_plan_link_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    meal_plan_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    meal_plan_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=True,
    )
    breakfast_selected = serializers.BooleanField(required=False, default=False)
    core_customer_user_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    contact_name = serializers.CharField(max_length=255)
    contact_phone = serializers.CharField(max_length=64)
    contact_email = serializers.EmailField(required=False, allow_blank=True)
    guest_market = serializers.ChoiceField(choices=RatePlan.GuestMarket.choices, default=RatePlan.GuestMarket.LOCAL)
    adults = serializers.IntegerField(min_value=1, max_value=100, default=1)
    children = serializers.IntegerField(min_value=0, max_value=100, default=0)
    extra_beds = serializers.IntegerField(min_value=0, max_value=20, default=0)
    add_ons = RequestedAddOnSerializer(many=True, required=False)
    preferences = RequestedRoomPreferenceSerializer(required=False, default=dict)
    guests = RequestedGuestSerializer(many=True, allow_empty=False)
    special_request = serializers.CharField(required=False, allow_blank=True)
    payment = WalkInPaymentSerializer(required=False, allow_null=True)
    rooms = WalkInRoomSerializer(many=True, required=False, allow_empty=False)

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError({"check_out": "Must be after check_in."})
        if (attrs["check_out"] - attrs["check_in"]).days > 90:
            raise serializers.ValidationError({"check_out": "A stay cannot exceed 90 nights."})
        has_legacy_room = attrs.get("physical_room_id") is not None
        has_rooms = bool(attrs.get("rooms"))
        if has_legacy_room == has_rooms:
            raise serializers.ValidationError({
                "rooms": "Send either rooms or the legacy physical_room_id fields, not both."
            })
        if has_rooms and any(
            field in self.initial_data
            for field in [
                "rate_plan_id", "meal_plan_link_id", "meal_plan_id", "meal_plan_ids", "breakfast_selected",
                "adults", "children", "extra_beds", "preferences",
            ]
        ):
            raise serializers.ValidationError({
                "rooms": "When rooms is provided, send room-specific fields inside each room."
            })
        if has_legacy_room and sum(bool(attrs.get(field)) for field in (
            "meal_plan_id", "meal_plan_link_id", "meal_plan_ids",
        )) > 1:
            raise serializers.ValidationError({
                "meal_plan_ids": "Send only one of meal_plan_ids, meal_plan_id, or meal_plan_link_id."
            })
        room_ids = [item["physical_room_id"] for item in attrs.get("rooms", [])]
        if len(room_ids) != len(set(room_ids)):
            raise serializers.ValidationError({"rooms": "The same physical room cannot be selected twice."})
        return attrs


class PaymentCreateSerializer(serializers.Serializer):
    class PaymentType:
        DEPOSIT = Payment.Type.DEPOSIT
        BALANCE = Payment.Type.BALANCE
        FULL_PAYMENT = Payment.Type.FULL_PAYMENT
        CHOICES = Payment.Type.choices

    payment_type = serializers.ChoiceField(choices=PaymentType.CHOICES, default=PaymentType.DEPOSIT)
    invoice_id = serializers.UUIDField(required=False)
    provider = serializers.CharField(max_length=50)
    provider_reference = serializers.CharField(max_length=255, required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=Payment.Status.choices, default=Payment.Status.PAID)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False)
    metadata = serializers.JSONField(required=False, default=dict)

    def validate(self, attrs):
        booking = self.context.get("booking")
        payment_type = attrs["payment_type"]
        invoice = None
        if booking and attrs.get("invoice_id"):
            invoice = booking.invoices.prefetch_related("receipts").filter(id=attrs["invoice_id"]).first()
            if not invoice:
                raise serializers.ValidationError({"invoice_id": "Invoice does not belong to this booking."})
        if booking and invoice is None:
            invoice = next(
                (item for item in booking.invoices.prefetch_related("receipts").order_by("issued_at", "id") if item.balance > 0 and item.status != Invoice.Status.VOID),
                None,
            )
        amount_due = invoice.balance if invoice else (max(booking.grand_total - booking.amount_paid, Decimal("0")) if booking else None)
        if amount_due is not None and amount_due <= 0:
            raise serializers.ValidationError({"amount": "This booking is already fully paid."})
        if payment_type == self.PaymentType.DEPOSIT:
            if "amount" not in attrs:
                raise serializers.ValidationError({"amount": "Amount is required for a deposit."})
        else:
            if "amount" not in attrs:
                attrs["amount"] = amount_due
            elif amount_due is not None and attrs["amount"] != amount_due:
                raise serializers.ValidationError({"amount": "Balance/full payment must equal the remaining amount due."})
        attrs["metadata"] = {**attrs.get("metadata", {}), "payment_type": payment_type}
        return attrs


class CorePaymentSuccessSerializer(serializers.Serializer):
    booking_id = serializers.UUIDField(required=False)
    booking_public_token = serializers.UUIDField(required=False)
    business_id = serializers.IntegerField(min_value=1)
    payment = serializers.DictField()
    aya = serializers.DictField(required=False, default=dict)

    def validate(self, attrs):
        if not attrs.get("booking_id") and not attrs.get("booking_public_token"):
            raise serializers.ValidationError("booking_id or booking_public_token is required.")

        payment = attrs.get("payment") or {}
        try:
            attrs["amount"] = Decimal(str(payment["amount"]))
        except (KeyError, TypeError, ValueError):
            raise serializers.ValidationError({"payment.amount": "A valid payment amount is required."})
        if attrs["amount"] <= 0:
            raise serializers.ValidationError({"payment.amount": "Payment amount must be greater than zero."})

        attrs["currency"] = str(payment.get("currency") or "MMK")
        attrs["payment_reference"] = str(payment.get("payment_reference") or payment.get("provider_payment_id") or "")
        if not attrs["payment_reference"]:
            raise serializers.ValidationError({"payment.payment_reference": "Payment reference is required."})
        return attrs


class RefundCreateSerializer(serializers.Serializer):
    payment_id = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    provider_reference = serializers.CharField(max_length=255, required=False, allow_blank=True)


class RoomAssignmentCreateSerializer(serializers.Serializer):
    booking_room_id = serializers.IntegerField(min_value=1)
    physical_room_id = serializers.IntegerField(min_value=1)


class RoomUnassignmentSerializer(serializers.Serializer):
    assignment_id = serializers.IntegerField(min_value=1)


class RoomChangeSerializer(serializers.Serializer):
    assignment_id = serializers.IntegerField(min_value=1)
    physical_room_id = serializers.IntegerField(min_value=1)


class RoomAssignmentSerializer(serializers.ModelSerializer):
    room_number = serializers.CharField(source="physical_room.room_number", read_only=True)

    class Meta:
        model = RoomAssignment
        fields = "__all__"
