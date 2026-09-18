"""Test settings: fast, hermetic, and free of network dependencies."""

from config.settings.base import *

DEBUG = False
ALLOWED_HOSTS = ["*"]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

LOGGING = {"version": 1, "disable_existing_loggers": True, "root": {"handlers": []}}
