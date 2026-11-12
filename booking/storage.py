from datetime import timedelta

from django.core.files.storage import storages
from django.utils import timezone


def get_private_document_storage():
    """Resolve the configured private backend for the current environment."""
    return storages["private"]


def coupon_image_url(image, request=None):
    if not image:
        return None

    url = image.url
    if getattr(image.storage, "custom_domain", None):
        storage = get_private_document_storage()
        signer = getattr(storage, "cloudfront_signer", None)
        if signer is not None:
            # Sign the existing object URL without adding the private storage prefix.
            expires_at = timezone.now() + timedelta(seconds=storage.querystring_expire)
            url = signer.generate_presigned_url(url, date_less_than=expires_at)

    return request.build_absolute_uri(url) if request else url
