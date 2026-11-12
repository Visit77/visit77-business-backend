from decimal import Decimal, ROUND_HALF_UP
from secrets import token_hex

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from booking.models import Booking, BookingCoupon, CouponCampaign, Hotel, Invoice
from booking.money_display import round_mmk


def active_campaigns(now=None):
    now = now or timezone.now()
    return CouponCampaign.objects.filter(
        type="booking", is_active=True, publish_at__lte=now, start_at__lte=now, end_at__gte=now,
    )


def campaign_matches_hotel(campaign, hotel):
    if (
        not hotel.is_active or not hotel.public_booking_enabled
        or hotel.package not in {Hotel.Package.OTA, Hotel.Package.OTA_PMS}
        or campaign.currency != hotel.base_currency
        or (campaign.business_ids and hotel.core_business_id not in campaign.business_ids)
    ):
        return False
    snapshot = hotel.core_snapshot or {}
    locations = snapshot.get("coupon_locations") or [{
        "country_id": snapshot.get("country_id"),
        "state_id": (snapshot.get("state") or {}).get("id"),
        "city_id": (snapshot.get("city") or {}).get("id"),
    }]
    return any(
        location.get("country_id") == campaign.country_id
        and (not campaign.state_ids or location.get("state_id") in campaign.state_ids)
        and (not campaign.city_ids or location.get("city_id") in campaign.city_ids)
        for location in locations
    )


def issue_coupons(campaign, user_id):
    """Caller locks the campaign. One claim issues all the user's tickets."""
    existing = list(campaign.coupons.filter(user_id=user_id).order_by("slot"))
    if existing:
        return existing
    return BookingCoupon.objects.bulk_create([
        BookingCoupon(campaign=campaign, user_id=user_id, slot=slot, code=token_hex(12).upper())
        for slot in range(1, campaign.quantity_per_user + 1)
    ])


def booking_coupon_discount(data, hotel, room_subtotal, total, *, booking=None):
    coupon_id = data.get("coupon_id")
    code = data.get("coupon_code")
    if not (coupon_id or code):
        return Decimal("0")
    user_id = data.get("booked_by_core_user_id")
    if not user_id or (coupon_id and code) or data.get("source", Booking.Source.OTA) != Booking.Source.OTA:
        raise ValidationError({"coupon": "Log in and select one coupon for an OTA booking."})

    tickets = BookingCoupon.objects.select_related("campaign").filter(user_id=user_id)
    if booking is not None:
        tickets = tickets.select_for_update(of=("self",))
    coupon = tickets.filter(pk=coupon_id).first() if coupon_id else tickets.filter(code=code.upper()).first()
    if (
        not coupon or coupon.used_at or coupon.booking_id
        or not active_campaigns().filter(pk=coupon.campaign_id).exists()
        or (coupon.campaign.audience == "targeted" and user_id not in coupon.campaign.user_ids)
        or not campaign_matches_hotel(coupon.campaign, hotel)
    ):
        raise ValidationError({"coupon": "This coupon is unavailable for this booking."})
    campaign = coupon.campaign
    discount = campaign.discount_value
    if campaign.discount_type == "percentage":
        discount = (room_subtotal * discount / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    discount = min(discount, room_subtotal, total)
    applied = round_mmk(total, hotel.base_currency) - round_mmk(total - discount, hotel.base_currency)
    if applied <= 0:
        raise ValidationError({"coupon": "This coupon has no value on this booking."})
    if booking is not None:
        coupon.booking = booking
        coupon.save(update_fields=["booking"])
    return applied


def release_booking_coupon(booking):
    BookingCoupon.objects.filter(booking=booking, used_at__isnull=True).update(booking=None)


def consume_booking_coupon(invoice):
    if invoice.charge_scope == Invoice.ChargeScope.OTA and invoice.coupon_discount_amount:
        BookingCoupon.objects.filter(booking_id=invoice.booking_id, used_at__isnull=True).update(used_at=timezone.now())
