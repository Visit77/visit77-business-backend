import hashlib
import hmac
import re
import requests

from django.conf import settings


def normalize_sms_phone_number(phone_no):
    """Return the Myanmar local format expected by the SMS gateway."""
    raw_value = str(phone_no or "").strip()
    digits = re.sub(r"\D", "", raw_value)
    if digits.startswith("959"):
        return f"0{digits[2:]}"
    if digits.startswith("9"):
        return f"0{digits}"
    return digits


def generate_sms_hash(phone_no):
    value = f"{phone_no}{settings.CUSTOM_SMS_APP_ID}"

    return hmac.new(
        settings.CUSTOM_SMS_SECRET_KEY.encode("utf-8"),
        msg=value.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def send_custom_sms(phone_no, message):
    phone_no = normalize_sms_phone_number(phone_no)
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

    if not response.ok:
        error_body = response.text.strip()
        raise requests.HTTPError(
            f"{response.status_code} SMS API error for {phone_no}: "
            f"{error_body or response.reason}",
            response=response,
        )

    return response.json()
