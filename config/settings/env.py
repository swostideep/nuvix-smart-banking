"""Tiny typed accessors over ``os.environ``.

Deliberately dependency-free: settings modules import this before anything
else is configured, and a misread environment variable should fail loudly at
boot rather than silently at 3am.
"""

from __future__ import annotations

import os

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ImproperlyConfigured(Exception):
    """Raised when an environment variable is present but unusable."""


def env_str(key: str, default: str | None = None) -> str:
    value = os.environ.get(key)
    if value is None or value == "":
        if default is None:
            raise ImproperlyConfigured(f"Missing required environment variable {key!r}")
        return default
    return value


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ImproperlyConfigured(f"{key!r} must be a boolean, got {raw!r}")


def env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{key!r} must be an integer, got {raw!r}") from exc


def env_list(key: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]
