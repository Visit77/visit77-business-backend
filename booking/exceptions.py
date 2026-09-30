from rest_framework.exceptions import PermissionDenied
from rest_framework.views import exception_handler

from config.response_formatter import fail


class TrialExpired(PermissionDenied):
    default_detail = (
        "Your Direct Booking trial has expired. "
        "Subscribe to continue using hotel actions."
    )
    default_code = "trial_expired"


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        error_data = response.data
        response_data = []
        if isinstance(error_data, dict) and any(
            key in error_data for key in ["conflict_bookings", "conflict_dates"]
        ):
            error_data = error_data.copy()
            response_data = {}
            for key in ["conflict_bookings", "conflict_dates"]:
                if key in error_data:
                    response_data[key] = error_data.pop(key)
        formatted = fail(
            error=error_data,
            args=response_data,
            status_code=response.status_code,
            error_type=(
                "trial_expired" if isinstance(exc, TrialExpired) else None
            ),
        )
        for header, value in response.items():
            formatted[header] = value
        return formatted
    return None
