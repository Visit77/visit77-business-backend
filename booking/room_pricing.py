"""Resolve room campaigns by stay date, market and currency."""


def discounted_room_amounts(base_price, usd_price, discounts, stay_date, guest_market, base_currency="MMK"):
    if guest_market not in {"local", "foreign"}:
        return base_price, usd_price
    def amount(original, suffix):
        field = f"{guest_market}_{suffix}_price"
        value = next((getattr(discount, field) for discount in discounts
                      if discount.ended_at is None and discount.start_date <= stay_date
                      and (discount.end_date is None or stay_date <= discount.end_date)
                      and getattr(discount, field) is not None), None)
        # An existing cheaper rate must never become more expensive.
        return min(original, value) if original is not None and value is not None else original
    return amount(base_price, "usd" if base_currency == "USD" else "mmk"), amount(usd_price, "usd")


def guest_price_fields(original_price, payable_price):
    """Describe a campaign reduction for one explicit currency's amounts."""
    from decimal import Decimal, ROUND_HALF_UP

    has_discount = original_price is not None and original_price > 0 and payable_price < original_price
    percentage = (
        ((original_price - payable_price) * Decimal("100") / original_price)
        .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if has_discount else Decimal("0")
    )
    return {
        "has_discount": has_discount,
        "original_price": original_price,
        "discount_percentage": percentage,
        "discount_price": payable_price if has_discount else None,
    }


def extra_bed_currency_rates(room_type, market):
    """Room-type owned rates; absent foreign currency rates inherit local rates."""
    def amount(currency):
        value = getattr(room_type, f'extra_bed_{market}_{currency}_price')
        if value is None and market == 'foreign':
            value = getattr(room_type, f'extra_bed_local_{currency}_price')
        return value

    return {'MMK': amount('mmk'), 'USD': amount('usd')}


def extra_bed_amounts(room_type, market):
    from decimal import Decimal
    rates = extra_bed_currency_rates(room_type, market)
    return rates['MMK'] or Decimal('0'), rates['USD']


def extra_bed_price_payload(room_type, market, display_currency=None):
    base, usd = extra_bed_amounts(room_type, market)
    show_usd = display_currency == 'USD' and usd is not None
    return {
        'base_price': base, 'base_currency': 'MMK',
        'usd_display_price': usd,
        'display_price': usd if show_usd else base,
        'display_currency': 'USD' if show_usd else 'MMK',
        'pricing_unit': 'per_bed_per_night',
    }


def validate_extra_bed_settlement(room_type, market, count):
    if not count:
        return
    from rest_framework.exceptions import ValidationError

    rates = extra_bed_currency_rates(room_type, market)
    if rates['MMK'] is None and rates['USD']:
        raise ValidationError({'extra_beds': 'Set the extra bed price in MMK before booking.'})
    if rates['MMK'] and room_type.hotel.base_currency != 'MMK':
        raise ValidationError({'extra_beds': 'Paid extra beds require an MMK booking.'})


def sync_market_price(room_type, market):
    """Keep the standard market rate in sync without rewriting custom offers."""
    from decimal import Decimal
    from booking.models import RatePlan
    plan = room_type.rate_plans.filter(guest_market=market, is_default=True).order_by("-is_active", "id").first()
    if plan is None:
        code = f"{market}-room-price"
        suffix = 1
        while room_type.rate_plans.filter(code=code).exists():
            suffix += 1
            code = f"{market}-room-price-{suffix}"
        plan = RatePlan(room_type=room_type, code=code, name=f"{market.title()} Room Price",
                        guest_market=market, is_default=True, source=RatePlan.Source.BOOKING,
                        currency=room_type.hotel.base_currency, default_price=0)
    mmk = getattr(room_type, f"{market}_mmk_price")
    usd = getattr(room_type, f"{market}_usd_price")
    plan.base_price = (usd if room_type.hotel.base_currency == "USD" else mmk) or Decimal("0")
    plan.default_price = plan.base_price
    plan.usd_display_price = usd
    # USD display alone must never publish a zero-price MMK booking.
    plan.is_active = plan.base_price > 0
    plan.save()
    return plan




def room_price_today(room_type):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from django.utils import timezone

    try:
        zone = ZoneInfo(room_type.hotel.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return timezone.localdate(timezone=zone)


def market_default_prices(room_type, stay_date, discounts):
    """Normal Booking setup and campaign reductions for both markets/currencies."""
    result = {}
    base_currency = room_type.hotel.base_currency
    campaigns = discounts if room_type.room_price_setup else []
    for market in ("local", "foreign"):
        normal_base = getattr(room_type, f"{market}_{'usd' if base_currency == 'USD' else 'mmk'}_price")
        normal_usd = getattr(room_type, f"{market}_usd_price")
        base, usd = discounted_room_amounts(normal_base, normal_usd, campaigns, stay_date, market, base_currency)
        usd_promo = guest_price_fields(normal_usd, usd)
        result[market] = {
            "base_price": normal_base,
            "base_currency": base_currency,
            "usd_display_price": normal_usd,
            "price_date": str(stay_date),
            **guest_price_fields(normal_base, base),
            **{f"usd_{field}": value for field, value in usd_promo.items()},
        }
    return result


def visible_room_discounts(room_type):
    """Return active/upcoming campaigns while reusing any prefetched rows."""
    from django.utils import timezone

    today = timezone.localdate()
    return [discount for discount in room_type.price_discounts.all()
            if discount.ended_at is None
            and (discount.end_date is None or discount.end_date >= today)]
