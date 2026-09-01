import hashlib
import hmac
import requests

from django.conf import settings


def generate_sms_hash(phone_no):
    value = f"{phone_no}{settings.CUSTOM_SMS_APP_ID}"

    return hmac.new(
        settings.CUSTOM_SMS_SECRET_KEY.encode("utf-8"),
        msg=value.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def send_custom_sms(phone_no, message):
    payload = {
        "phone_no": phone_no,
        "message": message,
        "app_id": settings.CUSTOM_SMS_APP_ID,
        "hash": generate_sms_hash(phone_no),
    }

    response = requests.post(
        settings.CUSTOM_SMS_URL,
        json=payload,
        timeout=15,
    )

    response.raise_for_status()

    return response.json()