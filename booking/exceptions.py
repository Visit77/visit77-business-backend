from rest_framework.views import exception_handler

from config.response_formatter import fail


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        formatted = fail(error=response.data, status_code=response.status_code)
        for header, value in response.items():
            formatted[header] = value
        return formatted
    return None
