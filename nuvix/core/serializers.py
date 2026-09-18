"""Serializer mixins shared across apps."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers


class ReadOnlyModelSerializer(serializers.ModelSerializer):
    """A serializer used purely for output."""

    def create(self, validated_data: dict[str, Any]) -> Any:  # pragma: no cover
        raise NotImplementedError("Read-only serializer")

    def update(self, instance: Any, validated_data: dict[str, Any]) -> Any:  # pragma: no cover
        raise NotImplementedError("Read-only serializer")


class MoneyField(serializers.DecimalField):
    """A currency amount: 2dp, never negative unless explicitly allowed."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_digits", 14)
        kwargs.setdefault("decimal_places", 2)
        super().__init__(**kwargs)


class RateField(serializers.DecimalField):
    """An APR expressed as a percentage (``18.99`` means 18.99%)."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_digits", 6)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("min_value", 0)
        kwargs.setdefault("max_value", 100)
        super().__init__(**kwargs)
