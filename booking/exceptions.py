from rest_framework.views import exception_handler

from config.response_formatter import fail


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        error_data = response.data
        response_data = []
        if isinstance(error_data, dict) and "conflict_bookings" in error_data:
            error_data = error_data.copy()
            response_data = {
                "conflict_bookings": error_data.pop("conflict_bookings"),
            }
        formatted = fail(
            error=error_data,
            args=response_data,
            status_code=response.status_code,
        )
        for header, value in response.items():
            formatted[header] = value
        return formatted
    return None
