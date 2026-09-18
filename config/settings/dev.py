"""Local development: loud errors, no external infrastructure required."""

from config.settings.base import *
from config.settings.base import NUVIX, REST_FRAMEWORK  # noqa: F401

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Run without Redis on a laptop.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True

REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
}
