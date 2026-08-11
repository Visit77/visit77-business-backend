import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models


class Hotel(models.Model):
    """Read-only projection of a Visit77 Core business."""

    class Package(models.TextChoices):
        FREE = "free", "Free Hotel"
        PMS = "pms", "PMS Only"
        OTA = "ota", "OTA Only"
        OTA_PMS = "ota_pms", "OTA + PMS"

    core_business_id = models.PositiveBigIntegerField(unique=True)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=64, blank=True)
    cover_image_url = models.URLField(max_length=1000, blank=True)
    base_currency = models.CharField(max_length=3, default="MMK")
    package = models.CharField(max_length=24, choices=Package.choices, default=Package.OTA)
    features = models.JSONField(default=dict, blank=True)
    check_in_time = models.TimeField(null=True, blank=True)
    check_out_time = models.TimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    core_snapshot = models.JSONField(default=dict, blank=True)
    access_snapshot = models.JSONField(default=dict, blank=True)
    core_updated_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name

    def has_feature(self, key):
        return bool((self.features or {}).get(key))


class RoomType(models.Model):
    """Core owns descriptive data; this service owns sellability and inventory."""

    class BreakfastPlanType(models.TextChoices):
        NO_BREAKFAST = "no_breakfast", "No Breakfast"
        INCLUDED_IN_ROOM_PRICE = "included_in_room_price", "Breakfast Included In Room Price"
        HOTEL_DEFAULT_PRICE = "hotel_default_price", "Use Hotel Default Breakfast Price"
        CUSTOM_PRICE = "custom_price", "Use Custom Breakfast Price"

    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE, related_name="room_types")
    core_room_type_id = models.PositiveBigIntegerField()
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    cover_image_url = models.URLField(max_length=1000, blank=True)
    max_adults = models.PositiveSmallIntegerField(default=1)
    max_children = models.PositiveSmallIntegerField(default=0)
    max_occupancy = models.PositiveSmallIntegerField(default=1)
    breakfast_plan_type = models.CharField(max_length=32, choices=BreakfastPlanType.choices, default=BreakfastPlanType.NO_BREAKFAST)
    breakfast_custom_local_base_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    breakfast_custom_local_usd_display_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    breakfast_custom_foreign_base_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    breakfast_custom_foreign_usd_display_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    default_inventory = models.PositiveSmallIntegerField(default=0)
    booking_enabled = models.BooleanField(default=True)
    core_active = models.BooleanField(default=True)
    core_snapshot = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["hotel", "core_room_type_id"], name="uniq_core_room_type_per_hotel")]

    def __str__(self):
        return f"{self.hotel.name} / {self.name}"


class MealPlan(models.Model):
    class Availability(models.TextChoices):
        GUEST_ONLY = "guest_only", "Guest Only"
        PUBLIC = "public", "Public / Walk-in"

    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE, related_name="meal_plans")
    core_meal_plan_id = models.PositiveBigIntegerField()
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    included_meals = models.JSONField(default=list, blank=True)
    meal_windows = models.JSONField(default=dict, blank=True)
    availability = models.CharField(max_length=32, choices=Availability.choices, default=Availability.GUEST_ONLY)
    local_base_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    local_usd_display_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    foreign_base_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    foreign_usd_display_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    is_default_for_room_type_breakfast = models.BooleanField(default=False)
    core_active = models.BooleanField(default=True)
    core_snapshot = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["hotel", "core_meal_plan_id"], name="uniq_core_meal_plan_per_hotel"),
            models.UniqueConstraint(fields=["hotel"], condition=models.Q(is_default_for_room_type_breakfast=True), name="uniq_booking_default_breakfast_per_hotel"),
        ]

    def __str__(self):
        return f"{self.hotel.name} / {self.name}"

    @property
    def includes_breakfast(self):
        return "breakfast" in (self.included_meals or [])


class RoomTypeMealPlan(models.Model):
    class PricingMode(models.TextChoices):
        HOTEL_DEFAULT = "hotel_default", "Use Hotel Default Meal Price"
        CUSTOM = "custom", "Use Custom Room Type Meal Price"
        INCLUDED_IN_ROOM_PRICE = "included_in_room_price", "Included In Room Price"

    room_type = models.ForeignKey(RoomType, on_delete=models.CASCADE, related_name="meal_plan_links")
    meal_plan = models.ForeignKey(MealPlan, on_delete=models.PROTECT, related_name="room_type_links")
    is_included = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)
    is_guest_selectable = models.BooleanField(default=True)
    use_hotel_default_price = models.BooleanField(default=True)
    pricing_mode = models.CharField(max_length=32, choices=PricingMode.choices, default=PricingMode.HOTEL_DEFAULT)
    local_base_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    local_usd_display_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    foreign_base_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    foreign_usd_display_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    core_snapshot = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["room_type", "meal_plan"], name="uniq_booking_room_type_meal_plan"),
        ]

    @property
    def is_price_included_in_room_price(self):
        return self.pricing_mode == self.PricingMode.INCLUDED_IN_ROOM_PRICE or self.is_included

    @property
    def effective_local_base_price(self):
        if self.is_price_included_in_room_price:
            return Decimal("0")
        return self.meal_plan.local_base_price if self.use_hotel_default_price else self.local_base_price

    @property
    def effective_local_usd_display_price(self):
        if self.is_price_included_in_room_price:
            return Decimal("0")
        return self.meal_plan.local_usd_display_price if self.use_hotel_default_price else self.local_usd_display_price

    @property
    def effective_foreign_base_price(self):
        if self.is_price_included_in_room_price:
            return Decimal("0")
        return self.meal_plan.foreign_base_price if self.use_hotel_default_price else self.foreign_base_price

    @property
    def effective_foreign_usd_display_price(self):
        if self.is_price_included_in_room_price:
            return Decimal("0")
        return self.meal_plan.foreign_usd_display_price if self.use_hotel_default_price else self.foreign_usd_display_price


class PhysicalRoom(models.Model):
    class Status(models.TextChoices):
        VACANT = "vacant", "Vacant"
        OCCUPIED = "occupied", "Occupied"
        CLEANING = "cleaning", "Cleaning"
        OUT_OF_SERVICE = "out_of_service", "Out of service"

    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE, related_name="physical_rooms")
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="physical_rooms")
    core_physical_room_id = models.PositiveBigIntegerField(null=True, blank=True)
    core_building_id = models.PositiveBigIntegerField(null=True, blank=True)
    core_floor_id = models.PositiveBigIntegerField(null=True, blank=True)
    room_number = models.CharField(max_length=50)
    floor = models.CharField(max_length=50, blank=True)
    building = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.VACANT)
    note = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    core_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["building", "floor", "room_number"]
        constraints = [models.UniqueConstraint(fields=["hotel", "room_number"], name="uniq_room_number_per_hotel")]
        indexes = [
            models.Index(fields=["hotel", "core_building_id"], name="booking_phy_hotel_i_2e5f4d_idx"),
            models.Index(fields=["hotel", "core_floor_id"], name="booking_phy_hotel_i_c2a08a_idx"),
        ]

    def clean(self):
        if self.room_type_id and self.hotel_id and self.room_type.hotel_id != self.hotel_id:
            raise ValidationError("Room type must belong to the same hotel.")


class RatePlan(models.Model):
    class Source(models.TextChoices):
        CORE = "core", "Core generated"
        BOOKING = "booking", "Booking Engine"

    class GuestMarket(models.TextChoices):
        ALL = "all", "All"
        LOCAL = "local", "Local"
        FOREIGN = "foreign", "Foreign"

    room_type = models.ForeignKey(RoomType, on_delete=models.CASCADE, related_name="rate_plans")
    core_rate_plan_id = models.CharField(max_length=120, blank=True, default="")
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.BOOKING)
    is_default = models.BooleanField(default=False)
    code = models.SlugField(max_length=80)
    name = models.CharField(max_length=255)
    guest_market = models.CharField(max_length=16, choices=GuestMarket.choices, default=GuestMarket.ALL)
    base_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    usd_display_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    extra_bed_base_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    extra_bed_usd_display_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="MMK")
    default_price = models.DecimalField(max_digits=14, decimal_places=2)
    extra_bed_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    breakfast_included = models.BooleanField(default=False)
    refundable = models.BooleanField(default=True)
    cancellation_policy = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["room_type", "code"], name="uniq_rate_plan_code"),
            models.UniqueConstraint(
                fields=["core_rate_plan_id"],
                condition=~models.Q(core_rate_plan_id=""),
                name="uniq_nonblank_core_rate_plan_id",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.base_price == 0 and self.default_price:
            self.base_price = self.default_price
        if self.default_price != self.base_price:
            self.default_price = self.base_price
        if self.extra_bed_base_price == 0 and self.extra_bed_price:
            self.extra_bed_base_price = self.extra_bed_price
        if self.extra_bed_price != self.extra_bed_base_price:
            self.extra_bed_price = self.extra_bed_base_price
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            if "default_price" in update_fields or "base_price" in update_fields:
                update_fields.update({"default_price", "base_price"})
            if "extra_bed_price" in update_fields or "extra_bed_base_price" in update_fields:
                update_fields.update({"extra_bed_price", "extra_bed_base_price"})
            kwargs["update_fields"] = update_fields
        super().save(*args, **kwargs)


class DailyInventory(models.Model):
    room_type = models.ForeignKey(RoomType, on_delete=models.CASCADE, related_name="daily_inventory")
    stay_date = models.DateField()
    total_rooms = models.PositiveSmallIntegerField()
    held_rooms = models.PositiveSmallIntegerField(default=0)
    reserved_rooms = models.PositiveSmallIntegerField(default=0)
    stop_sell = models.BooleanField(default=False)

    class Meta:
        ordering = ["stay_date"]
        constraints = [models.UniqueConstraint(fields=["room_type", "stay_date"], name="uniq_inventory_day")]
        indexes = [models.Index(fields=["room_type", "stay_date"])]

    @property
    def available_rooms(self):
        if self.stop_sell:
            return 0
        return max(self.total_rooms - self.held_rooms - self.reserved_rooms, 0)


class DailyRate(models.Model):
    rate_plan = models.ForeignKey(RatePlan, on_delete=models.CASCADE, related_name="daily_rates")
    stay_date = models.DateField()
    base_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    usd_display_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    price = models.DecimalField(max_digits=14, decimal_places=2)
    min_stay = models.PositiveSmallIntegerField(default=1)
    closed_to_arrival = models.BooleanField(default=False)
    closed_to_departure = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rate_plan", "stay_date"], name="uniq_daily_rate")]

    def save(self, *args, **kwargs):
        if self.base_price == 0 and self.price:
            self.base_price = self.price
        if self.price != self.base_price:
            self.price = self.base_price
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and ("price" in update_fields or "base_price" in update_fields):
            kwargs["update_fields"] = set(update_fields).union({"price", "base_price"})
        super().save(*args, **kwargs)


class RatePeriod(models.Model):
    """Effective-dated rate schedule; DailyRate remains the highest-priority override."""

    rate_plan = models.ForeignKey(RatePlan, on_delete=models.CASCADE, related_name="rate_periods")
    name = models.CharField(max_length=120)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Null means no end date.")
    base_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    usd_display_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    price = models.DecimalField(max_digits=14, decimal_places=2)
    min_stay = models.PositiveSmallIntegerField(default=1)
    closed_to_arrival = models.BooleanField(default=False)
    closed_to_departure = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_date", "id"]
        indexes = [models.Index(fields=["rate_plan", "start_date", "end_date"])]

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "Must be on or after start_date."})

    def save(self, *args, **kwargs):
        if self.base_price == 0 and self.price:
            self.base_price = self.price
        if self.price != self.base_price:
            self.price = self.base_price
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and ("price" in update_fields or "base_price" in update_fields):
            kwargs["update_fields"] = set(update_fields).union({"price", "base_price"})
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.rate_plan_id}: {self.name} ({self.start_date} - {self.end_date or 'open'})"


class AddOnTemplate(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    code = models.SlugField(max_length=80)
    version = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    allowed_pricing_units = models.JSONField(default=list)
    configuration_schema = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    created_by_core_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["code", "version"], name="uniq_add_on_template_version"),
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(status="published"),
                name="uniq_published_add_on_template_code",
            ),
        ]
        ordering = ["name", "-version", "id"]


class AddOnTemplateRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        REVIEWING = "reviewing", "Reviewing"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE, related_name="add_on_template_requests")
    requested_name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    suggested_pricing_units = models.JSONField(default=list, blank=True)
    suggested_schema = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    requested_by_core_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    reviewed_by_core_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    admin_note = models.TextField(blank=True)
    approved_template = models.ForeignKey(
        AddOnTemplate,
        on_delete=models.SET_NULL,
        related_name="approved_requests",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class AddOn(models.Model):
    class PricingUnit(models.TextChoices):
        PER_BOOKING = "per_booking", "Per booking"
        PER_NIGHT = "per_night", "Per night"
        PER_UNIT = "per_unit", "Per unit"

    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE, related_name="add_ons")
    template = models.ForeignKey(AddOnTemplate, on_delete=models.PROTECT, related_name="add_ons", null=True, blank=True)
    service_type = models.CharField(max_length=80, default="custom")
    code = models.SlugField(max_length=80)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    pricing_unit = models.CharField(max_length=20, choices=PricingUnit.choices, default=PricingUnit.PER_BOOKING)
    price = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="MMK")
    configuration_schema = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["hotel", "code"], name="uniq_add_on_code")]


class Booking(models.Model):
    class Source(models.TextChoices):
        OTA = "ota", "OTA"
        DIRECT = "direct", "Direct Booking"
        PHONE = "phone", "Phone / On-call"
        WALK_IN = "walk_in", "Walk-in"
        PMS = "pms", "PMS"

    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "Pending payment"
        CONFIRMED = "confirmed", "Confirmed"
        CHECKED_IN = "checked_in", "Checked in"
        CHECKED_OUT = "checked_out", "Checked out"
        CANCELED = "canceled", "Canceled"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=24, unique=True)
    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    hotel = models.ForeignKey(Hotel, on_delete=models.PROTECT, related_name="bookings")
    core_customer_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING_PAYMENT)
    source = models.CharField(max_length=24, choices=Source.choices, default=Source.DIRECT)
    source_name = models.CharField(max_length=120, blank=True)
    check_in = models.DateField()
    check_out = models.DateField()
    contact_name = models.CharField(max_length=255)
    contact_phone = models.CharField(max_length=64)
    contact_email = models.EmailField(blank=True)
    guest_market = models.CharField(max_length=16, choices=RatePlan.GuestMarket.choices, default=RatePlan.GuestMarket.LOCAL)
    currency = models.CharField(max_length=3, default="MMK")
    room_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    add_on_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cancellation_policy_snapshot = models.JSONField(default=dict, blank=True)
    special_request = models.TextField(blank=True)
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_in_by_core_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    check_in_verification_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["hotel", "idempotency_key"], condition=models.Q(idempotency_key__isnull=False), name="uniq_booking_idempotency")]
        indexes = [models.Index(fields=["hotel", "check_in", "check_out"]), models.Index(fields=["reference"])]

    @property
    def nights(self):
        return (self.check_out - self.check_in).days


class BookingRoom(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="rooms")
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="booking_rooms")
    rate_plan = models.ForeignKey(RatePlan, on_delete=models.PROTECT, related_name="booking_rooms")
    meal_plan_link = models.ForeignKey(RoomTypeMealPlan, on_delete=models.SET_NULL, related_name="booking_rooms", null=True, blank=True)
    quantity = models.PositiveSmallIntegerField(default=1)
    adults = models.PositiveSmallIntegerField(default=1)
    children = models.PositiveSmallIntegerField(default=0)
    extra_beds = models.PositiveSmallIntegerField(default=0)
    room_type_snapshot = models.JSONField(default=dict)
    rate_plan_snapshot = models.JSONField(default=dict)
    meal_plan_snapshot = models.JSONField(default=dict, blank=True)
    breakfast_snapshot = models.JSONField(default=dict, blank=True)
    preference_snapshot = models.JSONField(default=dict, blank=True)
    option_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    meal_plan_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    breakfast_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)


class BookingRoomNight(models.Model):
    booking_room = models.ForeignKey(BookingRoom, on_delete=models.CASCADE, related_name="nights")
    stay_date = models.DateField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    quantity = models.PositiveSmallIntegerField(default=1)
    extra_bed_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    option_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    meal_plan_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    breakfast_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["booking_room", "stay_date"], name="uniq_booking_room_night")]


class Guest(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="guests")
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=64, blank=True)
    email = models.EmailField(blank=True)
    nationality = models.CharField(max_length=100, blank=True)
    nrc_number = models.CharField(max_length=100, blank=True)
    passport_number = models.CharField(max_length=100, blank=True)
    identity_type = models.CharField(max_length=50, blank=True)
    identity_number = models.CharField(max_length=100, blank=True)
    is_primary = models.BooleanField(default=False)


class GuestIdentityDocument(models.Model):
    class DocumentType(models.TextChoices):
        IDENTITY_PHOTO = "identity_photo", "Identity Photo"

    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name="identity_documents")
    document_type = models.CharField(max_length=24, choices=DocumentType.choices)
    document_number = models.CharField(max_length=100, blank=True)
    file = models.FileField(upload_to="booking/guest-identities/%Y/%m/")
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by_core_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document_type", "id"]
        constraints = [
            models.UniqueConstraint(fields=["guest", "document_type"], name="uniq_guest_identity_document_type"),
        ]


class BookingAddOn(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="add_ons")
    add_on = models.ForeignKey(AddOn, on_delete=models.PROTECT, related_name="booking_add_ons")
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    configuration = models.JSONField(default=dict, blank=True)
    add_on_snapshot = models.JSONField(default=dict)


class RoomAssignment(models.Model):
    booking_room = models.ForeignKey(BookingRoom, on_delete=models.CASCADE, related_name="assignments")
    physical_room = models.ForeignKey(PhysicalRoom, on_delete=models.PROTECT, related_name="assignments")
    assigned_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)


class Payment(models.Model):
    class Type(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        FULL_PAYMENT = "full_payment", "Full payment"
        BALANCE = "balance", "Remaining balance"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially refunded"

    class Provider(models.TextChoices):
        DEMO = "demo", "Demo"
        AYA = "aya", "AYA"
        CASH = "cash", "Cash"
        MMQR = "mmqr", "MMQR"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="payments")
    payment_type = models.CharField(max_length=24, choices=Type.choices, default=Type.FULL_PAYMENT)
    provider = models.CharField(max_length=50, choices=Provider.choices)
    provider_reference = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    refunded_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=3)
    invoice_number = models.CharField(max_length=32, unique=True)
    receipt_number = models.CharField(max_length=32, unique=True, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CoreIntegrationEvent(models.Model):
    """Inbox deduplication for at-least-once Core outbox delivery."""

    event_id = models.UUIDField(unique=True)
    event_type = models.CharField(max_length=64)
    core_business_id = models.PositiveBigIntegerField()
    payload = models.JSONField(default=dict, blank=True)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-processed_at"]
