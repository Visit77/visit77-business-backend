from .base import *


DEBUG = True
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
DEMO_PAYMENT_ENABLED = env.bool("DEMO_PAYMENT_ENABLED", default=True)
