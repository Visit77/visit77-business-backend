"""Date-aware bulk price quotes for the Core directory (no inventory lock)."""
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.views import APIView

from booking.models import DailyRate, Hotel, RatePlan, RatePeriod, RoomType
from booking.permissions import HasBookingAdminKey
from booking.room_pricing import guest_price_fields
from booking.services import _rate_amounts
from config.response_formatter import success


class DirectoryPriceQuerySerializer(serializers.Serializer):
    business_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), max_length=5000, allow_empty=True)
    guest_market = serializers.ChoiceField(choices=["local", "foreign"], default="local")
    display_currency = serializers.ChoiceField(choices=["MMK", "USD"], default="MMK")
    min_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False)
    max_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False)

    def validate(self, attrs):
        if "min_price" in attrs and "max_price" in attrs and attrs["min_price"] > attrs["max_price"]:
            raise serializers.ValidationError("max_price must be greater than or equal to min_price.")
        return attrs


def minimum_room_prices_today(business_ids, guest_market="local", display_currency="MMK", min_price=None, max_price=None):
    hotels = list(Hotel.objects.filter(core_business_id__in=business_ids, is_active=True))
    today = {}
    for hotel in hotels:
        try:
            zone = ZoneInfo(hotel.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("UTC")
        today[hotel.id] = timezone.localdate(timezone=zone)
    rooms = list(RoomType.objects.filter(
        hotel_id__in=today, core_active=True, booking_enabled=True, room_price_setup=True,
    ).select_related("hotel").prefetch_related(
        "price_discounts", Prefetch("rate_plans", queryset=RatePlan.objects.filter(
            is_active=True, guest_market__in=[guest_market, "all"],
        ).order_by("id")),
    ).order_by("id"))
    plan_ids = [plan.id for room in rooms for plan in room.rate_plans.all()]
    days = set(today.values())
    daily = {(rule.rate_plan_id, rule.stay_date): rule for rule in DailyRate.objects.filter(rate_plan_id__in=plan_ids, stay_date__in=days)}
    periods = {}
    if days:
        for rule in RatePeriod.objects.filter(rate_plan_id__in=plan_ids, is_active=True, start_date__lte=max(days)).filter(Q(end_date__isnull=True) | Q(end_date__gte=min(days))).order_by("-start_date", "-id"):
            periods.setdefault(rule.rate_plan_id, []).append(rule)
    result = {}
    for room in rooms:
        day = today[room.hotel_id]
        discounts = list(room.price_discounts.all())
        for plan in room.rate_plans.all():
            rule = daily.get((plan.id, day)) or next((period for period in periods.get(plan.id, []) if period.start_date <= day and (period.end_date is None or day <= period.end_date)), None)
            base, usd = _rate_amounts(rule, plan, day, guest_market, discounts)
            original_base, original_usd = (rule.base_price, rule.usd_display_price) if rule else (plan.base_price, plan.usd_display_price)
            # Never label an MMK fallback as USD or convert using an invented FX rate.
            price = (base if room.hotel.base_currency == "USD" else usd) if display_currency == "USD" else base if room.hotel.base_currency == "MMK" else None
            original = (original_base if room.hotel.base_currency == "USD" else original_usd) if display_currency == "USD" else original_base if room.hotel.base_currency == "MMK" else None
            if price is None or original is None or price <= 0:
                continue
            if (min_price is not None and price < min_price) or (max_price is not None and price > max_price):
                continue
            business_id = room.hotel.core_business_id
            existing = result.get(business_id)
            if existing and existing["display_price"] <= price:
                continue
            result[business_id] = {
                "room_type_id": room.core_room_type_id,
                "booking_room_type_id": room.id,
                "room_type_name": room.name,
                "rate_plan_id": plan.id,
                "price_date": str(day),
                "guest_market": guest_market,
                "display_currency": display_currency,
                "display_price": price,
                **guest_price_fields(original, price),
                "local_base_price": room.local_mmk_price if room.hotel.base_currency == "MMK" else room.local_usd_price,
                "local_base_currency": room.hotel.base_currency,
                "local_usd_display_price": room.local_usd_price,
                "foreign_base_price": room.foreign_mmk_price if room.hotel.base_currency == "MMK" else room.foreign_usd_price,
                "foreign_base_currency": room.hotel.base_currency,
                "foreign_usd_display_price": room.foreign_usd_price,
            }
    return result


class AdminDirectoryMinimumPricesView(APIView):
    permission_classes = [HasBookingAdminKey]

    def post(self, request):
        serializer = DirectoryPriceQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        scope = getattr(request, "booking_core_business_id", None)
        if scope is not None:
            params["business_ids"] = [value for value in params["business_ids"] if value == scope]
        prices = minimum_room_prices_today(**params)
        return success({"minimum_rooms": {str(key): value for key, value in prices.items()}})
