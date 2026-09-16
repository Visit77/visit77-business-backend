from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response


_MONEY_EXACT_KEYS = {
    "price", "amount", "subtotal", "balance", "amount_due",
    "amount_paid", "usd_display", "discount", "taxes", "service_fee",
    "already_refunded", "refundable_remaining", "deposit", "tax", "fee",
}
_MONEY_KEY_SUFFIXES = (
    "_price", "_prices", "_amount", "_total", "_subtotal", "_balance",
    "_due", "_paid",
)
_NON_MONEY_EXACT_KEYS = {
    "ota_record_total",
}


def _is_monetary_key(key):
    key = str(key or "").lower()
    if (
        key in _NON_MONEY_EXACT_KEYS
        or key.startswith("formatted_")
        or key.endswith("_formatted")
    ):
        return False
    return key in _MONEY_EXACT_KEYS or key.endswith(_MONEY_KEY_SUFFIXES)


def normalize_monetary_response(value, monetary=False):
    """Return JSON-ready monetary values as floats without changing IDs/counts."""
    if isinstance(value, Mapping):
        total_is_monetary = any(
            key in value for key in ("currency", "base_currency", "unit_price")
        ) or any(
            str(key).lower() != "total" and _is_monetary_key(key)
            for key in value
        )
        return {
            key: normalize_monetary_response(
                item,
                _is_monetary_key(key)
                or (str(key).lower() == "total" and total_is_monetary),
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [normalize_monetary_response(item, monetary) for item in value]
    if not monetary or value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(Decimal(value.replace(",", "").strip()))
        except (InvalidOperation, ValueError):
            return value
    return value


def success(args, message='Success', extra_dict=None, status_code=status.HTTP_200_OK, status=status.HTTP_200_OK):
    response = _response_formatter(
        normalize_monetary_response(args),
        status_code=status_code,
        message=message,
    )
    if extra_dict:
        response.update(normalize_monetary_response(extra_dict))

    try:
        total_counts = args[-1]['total_counts']
        del args[-1]

        response["total_counts"] = total_counts
        return Response(normalize_monetary_response(response), status=status)
    except (KeyError, IndexError, TypeError):
        return Response(normalize_monetary_response(response), status=status)


def fail(error=None, status_code=status.HTTP_400_BAD_REQUEST, args=None):
    if args is None:
        args = []
    return Response(_response_formatter(args, status_code=status_code, error=error, message='Fail'),
                    status=status_code)


def _response_formatter(args, status_code=200, message='Success', error=None):
    if error is None:
        error = {}
    error_list = []
    
    if error:
        if isinstance(error, Mapping):
            for key, value in error.items():
                values = value if isinstance(value, (list, tuple)) else [value]
                for item in values:
                    value_str = str(item)
                    if key not in ['non_field_errors', 'detail']:
                        key_str = str(key).capitalize().replace('_', ' ')
                        error_list.append(f"{key_str} - {value_str}")
                    else:
                        error_list.append(value_str)
        elif isinstance(error, (list, tuple)):
            error_list.extend(str(item) for item in error)
        else:
            error_list.append(str(error))
    return {
        'count': len(args) if hasattr(args, '__len__') and args else 0,
        'message': message,
        'status_code': status_code,
        'data': args,
        'error': error_list,
    }


class ResponseJsonFormatter(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        status_code = renderer_context['response'].status_code

        is_success = 200 <= status_code < 300
        if is_success:
            if type(data) is dict:
                if renderer_context['request'].path == '/accounts/login/':
                    formatted_data = _response_formatter(data)
                else:
                    formatted_data = _response_formatter(data['data'])
            else:
                formatted_data = _response_formatter(data if data else [])
        else:
            formatted_data = _response_formatter(status_code=status_code, error=[], args=[])

        # print(formatted_data)
        return super(ResponseJsonFormatter, self).render(formatted_data, accepted_media_type, renderer_context)
