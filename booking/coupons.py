from decimal import Decimal, ROUND_HALF_UP
from secrets import token_hex

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from booking.models import Booking, BookingCoupon, CouponCampaign, Hotel, Invoice
from booking.money_display import round_mmk


def published_campaigns(now=None):
    now = now or timezone.now()
    return CouponCampaign.objects.filter(
        type="booking", is_active=True, publish_at__lte=now, end_at__gte=now,
    )


def active_campaigns(now=None):
    now = now or timezone.now()
    return published_campaigns(now).filter(start_at__lte=now)


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
    ids = data.get("coupon_ids") or []
    codes = data.get("coupon_codes") or []
    if data.get("coupon_id"):
        ids = [*ids, data["coupon_id"]]
    if data.get("coupon_code"):
        codes = [*codes, data["coupon_code"]]
    if not (ids or codes):
        return Decimal("0")

    user_id = data.get("booked_by_core_user_id")
    if not user_id or data.get("source", Booking.Source.OTA) != Booking.Source.OTA:
        raise ValidationError({"coupon": "Log in to use coupons for an OTA booking."})
    if ids and codes:
        raise ValidationError({"coupon": "Use coupon IDs or coupon codes in one request."})

    identifiers = [str(pk) for pk in ids] if ids else [code.upper() for code in codes]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError({"coupon": "Each coupon can only be selected once."})

    tickets = BookingCoupon.objects.select_related("campaign").filter(
        user_id=user_id, **{"pk__in" if ids else "code__in": identifiers},
    ).order_by("pk")
    if booking is not None:
        tickets = tickets.select_for_update(of=("self",))
    coupons = list(tickets)
    if len(coupons) != len(identifiers):
        raise ValidationError({"coupon": "A selected coupon is unavailable for this booking."})

    eligible_campaigns = set(active_campaigns().filter(
        pk__in={coupon.campaign_id for coupon in coupons},
    ).values_list("pk", flat=True))
    by_identifier = {str(coupon.pk) if ids else coupon.code: coupon for coupon in coupons}
    total_before_discount = round_mmk(total, hotel.base_currency)
    discount = applied = Decimal("0")
    snapshot = []
    for identifier in identifiers:
        coupon = by_identifier[identifier]
        campaign = coupon.campaign
        if (
            coupon.used_at or coupon.booking_id or campaign.pk not in eligible_campaigns
            or (campaign.audience == "targeted" and user_id not in campaign.user_ids)
            or not campaign_matches_hotel(campaign, hotel)
        ):
            raise ValidationError({"coupon": "A selected coupon is unavailable for this booking."})
        value = campaign.discount_value
        if campaign.discount_type == "percentage":
            value = (room_subtotal * value / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        discount = min(discount + value, room_subtotal, total)
        cumulative = total_before_discount - round_mmk(total - discount, hotel.base_currency)
        snapshot.append({"name": campaign.title, "amount": str(cumulative - applied)})
        applied = cumulative

    if applied <= 0:
        raise ValidationError({"coupon": "The selected coupons have no value on this booking."})
    if booking is not None:
        booking.coupon_discount_snapshot = snapshot
        BookingCoupon.objects.filter(pk__in=[coupon.pk for coupon in coupons]).update(booking=booking)
    return applied


def release_booking_coupon(booking):
    BookingCoupon.objects.filter(booking=booking, used_at__isnull=True).update(booking=None)


def consume_booking_coupon(invoice):
    if invoice.charge_scope == Invoice.ChargeScope.OTA and invoice.coupon_discount_amount:
        BookingCoupon.objects.filter(booking_id=invoice.booking_id, used_at__isnull=True).update(used_at=timezone.now())
