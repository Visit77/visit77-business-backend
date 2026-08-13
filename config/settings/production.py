from django.core.exceptions import ImproperlyConfigured

from .base import *


DEBUG = False
DEMO_PAYMENT_ENABLED = env.bool("DEMO_PAYMENT_ENABLED", default=False)
BOOKING_REQUIRE_BUSINESS_SCOPE = True

if SECRET_KEY == "dev-only-booking-engine-key":
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in production.")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production.")
if BOOKING_ADMIN_API_KEY == "change-me":
    raise ImproperlyConfigured("BOOKING_ADMIN_API_KEY must be set in production.")
if not CORE_SERVICE_KEY:
    raise ImproperlyConfigured("CORE_SERVICE_KEY must be set in production.")
if not CORE_JWT_SIGNING_KEY and not CORE_JWT_VERIFYING_KEY:
    raise ImproperlyConfigured("Core JWT verification key must be configured in production.")
if env("DB_ENGINE", default="sqlite") != "postgres":
    raise ImproperlyConfigured("Production Booking Engine must use PostgreSQL (DB_ENGINE=postgres).")
if not USE_S3_STORAGE:
    raise ImproperlyConfigured("Production Booking Engine must use S3 storage (USE_S3_STORAGE=true).")
if not AWS_S3_CUSTOM_DOMAIN:
    raise ImproperlyConfigured("AWS_S3_CUSTOM_DOMAIN must be configured for CloudFront media delivery.")
if not AWS_CLOUDFRONT_KEY_ID or not AWS_CLOUDFRONT_KEY:
    raise ImproperlyConfigured("CloudFront signing credentials must be configured for private documents.")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
