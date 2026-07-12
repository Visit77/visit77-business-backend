from collections.abc import Mapping

from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response


def success(args, message='Success', extra_dict=None, status_code=status.HTTP_200_OK, status=status.HTTP_200_OK):
    response = _response_formatter(args, status_code=status_code, message=message)
    if extra_dict:
        response.update(extra_dict)

    try:
        total_counts = args[-1]['total_counts']
        del args[-1]

        response["total_counts"] = total_counts
        return Response(response, status=status)
    except (KeyError, IndexError, TypeError):
        return Response(response, status=status)


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
