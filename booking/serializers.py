from decimal import Decimal

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
    Hotel,
    MealPlan,
    Payment,
    PhysicalRoom,
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
    class Meta:
        model = Hotel
        fields = "__all__"
        read_only_fields = [
            "core_business_id", "name", "slug", "address", "phone", "cover_image_url",
            "package", "features", "core_snapshot", "access_snapshot", "synced_at",
        ]

    def validate_base_currency(self, value):
        value = value.upper()
        if value not in {"MMK", "USD"}:
            raise serializers.ValidationError("Supported base currencies are MMK and USD.")
        return value


class PublicHotelSerializer(serializers.ModelSerializer):
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
            "package",
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
            "hotel", "core_meal_plan_id", "name", "description", "included_meals",
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
        read_only_fields = ["hotel", "room_type", "core_physical_room_id", "room_number", "floor", "building", "is_active"]

    def validate(self, attrs):
        hotel = attrs.get("hotel", getattr(self.instance, "hotel", None))
        room_type = attrs.get("room_type", getattr(self.instance, "room_type", None))
        if hotel and room_type and room_type.hotel_id != hotel.id:
            raise serializers.ValidationError("Room type must belong to the selected hotel.")
        return attrs


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
    class Meta:
        model = Payment
        fields = "__all__"


class BookingSerializer(serializers.ModelSerializer):
    rooms = BookingRoomSerializer(many=True, read_only=True)
    guests = GuestSerializer(many=True, read_only=True)
    add_ons = BookingAddOnSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    core_business_id = serializers.IntegerField(source="hotel.core_business_id", read_only=True)
    hotel_name = serializers.CharField(source="hotel.name", read_only=True)
    nights = serializers.IntegerField(read_only=True)
    hotel_id = serializers.IntegerField(source='hotel.id')

    class Meta:
        model = Booking
        # fields = "__all__"
        exclude = ["hotel"]
        


class RequestedRoomPreferenceSerializer(serializers.Serializer):
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
    breakfast_selected = serializers.BooleanField(required=False, default=False)
    quantity = serializers.IntegerField(min_value=1, max_value=20)
    adults = serializers.IntegerField(min_value=1, max_value=100, default=1)
    children = serializers.IntegerField(min_value=0, max_value=100, default=0)
    extra_beds = serializers.IntegerField(min_value=0, max_value=20, default=0)
    preferences = RequestedRoomPreferenceSerializer(required=False, default=dict)


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


class BookingEstimateSerializer(serializers.Serializer):
    core_business_id = serializers.IntegerField(min_value=1)
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    guest_market = serializers.ChoiceField(choices=RatePlan.GuestMarket.choices, default=RatePlan.GuestMarket.LOCAL)
    rooms = RequestedRoomSerializer(many=True, allow_empty=False)
    add_ons = RequestedAddOnSerializer(many=True, required=False)

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError({"check_out": "Must be after check_in."})
        if (attrs["check_out"] - attrs["check_in"]).days > 90:
            raise serializers.ValidationError({"check_out": "A stay cannot exceed 90 nights."})
        return attrs


class WalkInPaymentSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=Payment.Provider.choices, default=Payment.Provider.CASH)
    provider_reference = serializers.CharField(max_length=255, required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=[Payment.Status.PAID, Payment.Status.PENDING],
        default=Payment.Status.PAID,
    )
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, allow_null=True)
    metadata = serializers.JSONField(required=False, default=dict)


class WalkInBookingCreateSerializer(serializers.Serializer):
    physical_room_id = serializers.IntegerField(min_value=1)
    rate_plan_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    meal_plan_link_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
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
    preferences = RequestedRoomPreferenceSerializer(required=False, default=dict)
    guests = RequestedGuestSerializer(many=True, allow_empty=False)
    special_request = serializers.CharField(required=False, allow_blank=True)
    payment = WalkInPaymentSerializer(required=False, default=dict)

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError({"check_out": "Must be after check_in."})
        if (attrs["check_out"] - attrs["check_in"]).days > 90:
            raise serializers.ValidationError({"check_out": "A stay cannot exceed 90 nights."})
        guests = attrs.get("guests") or []
        guest_market = attrs.get("guest_market", RatePlan.GuestMarket.LOCAL)
        has_nrc_guest = any((guest.get("nrc_number") or "").strip() for guest in guests)
        has_identity_guest = any(
            (guest.get("identity_type") or "").strip() and (guest.get("identity_number") or "").strip()
            for guest in guests
        )
        if guest_market == RatePlan.GuestMarket.LOCAL and not has_nrc_guest:
            raise serializers.ValidationError({"guests": [{"nrc_number": "At least one guest NRC number is required for local walk-in bookings."}]})
        if guest_market == RatePlan.GuestMarket.FOREIGN:
            if not has_identity_guest:
                raise serializers.ValidationError(
                    {
                        "guests": [
                            {
                                "identity_type": "At least one guest identity type is required for foreigner walk-in bookings.",
                                "identity_number": "At least one guest identity number is required for foreigner walk-in bookings.",
                            }
                        ]
                    }
                )
        return attrs


class PaymentCreateSerializer(serializers.Serializer):
    provider = serializers.CharField(max_length=50)
    provider_reference = serializers.CharField(max_length=255, required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=Payment.Status.choices, default=Payment.Status.PAID)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    metadata = serializers.JSONField(required=False, default=dict)


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
