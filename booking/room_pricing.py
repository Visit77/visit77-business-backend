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
    """Describe a campaign reduction in the guest's selected display currency."""
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


