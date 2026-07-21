import os
from pathlib import Path

import environ
from celery.schedules import crontab


BASE_DIR = Path(__file__).resolve().parent.parent.parent
env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-booking-engine-key")
DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])
CORS_ALLOW_HEADERS = (
    "accept",
    "authorization",
    "content-type",
    "user-agent",
    "Access-Control-Allow-Origin",
    "x-requested-with", "Access-Control-Allow-Headers", "Origin", "Accept", "X-Requested-With",
    "Content-Type", "Access-Control-Request-Method", "Access-Control-Request-Headers",
    "Idempotency-Key",
    "idempotency-key",
    "X-Booking-Admin-Key","X-Booking-Business-ID"
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "booking",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

if env("DB_ENGINE", default="sqlite") == "postgres":
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", default="booking_engine"),
        "USER": env("DB_USER", default="postgres"),
        "PASSWORD": env("DB_PASSWORD", default=""),
        "HOST": env("DB_HOST", default="127.0.0.1"),
        "PORT": env("DB_PORT", default="5432"),
        "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=60),
        "CONN_HEALTH_CHECKS": True,
    }}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="Asia/Yangon")
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "EXCEPTION_HANDLER": "booking.exceptions.api_exception_handler",
}

CORE_BASE_URL = env("CORE_BASE_URL", default="http://127.0.0.1:8000").rstrip("/")
CORE_SERVICE_TOKEN = env("CORE_SERVICE_TOKEN", default="")
CORE_SERVICE_KEY = env("CORE_SERVICE_KEY", default="")
CORE_JWT_SIGNING_KEY = env("CORE_JWT_SIGNING_KEY", default="")
CORE_JWT_VERIFYING_KEY = env("CORE_JWT_VERIFYING_KEY", default="")
CORE_JWT_ALGORITHM = env("CORE_JWT_ALGORITHM", default="HS256")
CORE_JWT_AUDIENCE = env("CORE_JWT_AUDIENCE", default="")
CORE_JWT_ISSUER = env("CORE_JWT_ISSUER", default="")
BOOKING_ADMIN_API_KEY = env("BOOKING_ADMIN_API_KEY", default="change-me")
BOOKING_REQUIRE_BUSINESS_SCOPE = env.bool("BOOKING_REQUIRE_BUSINESS_SCOPE", default=False)
BOOKING_HOLD_MINUTES = env.int("BOOKING_HOLD_MINUTES", default=15)
BOOKING_INVENTORY_WINDOW_DAYS = env.int("BOOKING_INVENTORY_WINDOW_DAYS", default=365)
DEMO_PAYMENT_ENABLED = env.bool("DEMO_PAYMENT_ENABLED", default=False)

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://redis:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://redis:6379/1")
CELERY_TASK_TRACK_STARTED = True
CELERY_BEAT_SCHEDULE = {
    "expire-unpaid-booking-holds-every-minute": {
        "task": "booking.tasks.expire_booking_holds_task",
        "schedule": crontab(minute="*"),
    },
    "ensure-rolling-daily-inventory": {
        "task": "booking.tasks.ensure_rolling_daily_inventory_task",
        "schedule": crontab(hour=1, minute=0),
    },
}
