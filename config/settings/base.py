import os
from pathlib import Path

import environ
from celery.schedules import crontab
from corsheaders.defaults import default_headers


BASE_DIR = Path(__file__).resolve().parent.parent.parent
env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-booking-engine-key")
DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])
CORS_ALLOW_HEADERS = (*default_headers, "idempotency-key", "x-booking-admin-key", "x-booking-business-id")
CORS_ALLOW_ALL_ORIGINS = env.bool("CORS_ALLOW_ALL_ORIGINS", default=True)
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = env.bool("CORS_ALLOW_CREDENTIALS", default=False)

INSTALLED_APPS = [
    "corsheaders",
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
    "corsheaders.middleware.CorsMiddleware",
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
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_S3_STORAGE = env.bool("USE_S3_STORAGE", default=False)
if USE_S3_STORAGE:
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="ap-southeast-1")
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default=None)
    AWS_S3_CUSTOM_DOMAIN = env("AWS_S3_CUSTOM_DOMAIN", default="media.visit77.com")
    AWS_CLOUDFRONT_KEY_ID = env("AWS_CLOUDFRONT_KEY_ID", default="")
    AWS_CLOUDFRONT_KEY = env("AWS_CLOUDFRONT_KEY", default="").replace("\\n", "\n").strip()
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_EXPIRE = env.int("AWS_QUERYSTRING_EXPIRE", default=900)
    AWS_S3_FILE_OVERWRITE = False
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": AWS_STORAGE_BUCKET_NAME,
                "region_name": AWS_S3_REGION_NAME,
                "access_key": AWS_ACCESS_KEY_ID,
                "secret_key": AWS_SECRET_ACCESS_KEY,
                "endpoint_url": AWS_S3_ENDPOINT_URL,
                "custom_domain": AWS_S3_CUSTOM_DOMAIN,
                "default_acl": None,
                "file_overwrite": False,
                "querystring_auth": False,
                "object_parameters": {"CacheControl": "public, max-age=86400"},
            },
        },
        "private": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": AWS_STORAGE_BUCKET_NAME,
                "region_name": AWS_S3_REGION_NAME,
                "access_key": AWS_ACCESS_KEY_ID,
                "secret_key": AWS_SECRET_ACCESS_KEY,
                "endpoint_url": AWS_S3_ENDPOINT_URL,
                "custom_domain": AWS_S3_CUSTOM_DOMAIN,
                "cloudfront_key_id": AWS_CLOUDFRONT_KEY_ID or None,
                "cloudfront_key": AWS_CLOUDFRONT_KEY or None,
                "location": "private",
                "default_acl": None,
                "file_overwrite": False,
                "querystring_auth": True,
                "querystring_expire": AWS_QUERYSTRING_EXPIRE,
                "object_parameters": {"CacheControl": "private, no-store"},
            },
        },
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
else:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": MEDIA_ROOT, "base_url": MEDIA_URL},
        },
        "private": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": MEDIA_ROOT / "private", "base_url": f"{MEDIA_URL}private/"},
        },
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }

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
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "expire-unpaid-booking-holds-every-minute": {
        "task": "booking.tasks.expire_booking_holds_task",
        "schedule": crontab(minute="*"),
    },
    "ensure-rolling-daily-inventory": {
        "task": "booking.tasks.ensure_rolling_daily_inventory_task",
        "schedule": crontab(hour=1, minute=0),
    },
    "auto-cancel-no-show-reservations": {
        "task": "booking.tasks.auto_cancel_no_show_reservations_task",
        # Keep this idempotent cleanup frequent. A once-daily schedule can be
        # missed when beat is restarted around midnight, leaving yesterday's
        # no-shows confirmed and their rooms unavailable for another day.
        "schedule": crontab(minute="*/15"),
    },
}

LOG_LEVEL = env("DJANGO_LOG_LEVEL", default="INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(levelname)s %(asctime)s %(name)s || %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "booking": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "config": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}
