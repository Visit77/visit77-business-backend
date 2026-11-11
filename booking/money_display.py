"""MMK rounding and currency display for booking amounts."""
from decimal import Decimal, ROUND_HALF_UP


def round_mmk(value, currency):
    amount = Decimal(str(value or 0))
    if str(currency).upper() == 'MMK':
        return amount.to_integral_value(rounding=ROUND_HALF_UP)
    return amount


def format_amount(value, currency):
    amount = round_mmk(value, currency)
    return f'{int(amount):,}' if amount == amount.to_integral_value() else f'{amount:,.2f}'


def format_money(value, currency):
    return f'{currency} {format_amount(value, currency)}'
