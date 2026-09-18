"""Operational endpoints used by the load balancer and orchestrator."""

from __future__ import annotations

from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Liveness: is the process up? Deliberately touches no dependency."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        return Response({"status": "ok", "service": "nuvix-api"})


class ReadinessView(APIView):
    """Readiness: can this process actually serve traffic?

    Checks the database, because a pod that cannot reach Postgres should be
    pulled out of rotation rather than returning 500s to customers.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(responses={200: dict, 503: dict})
    def get(self, request: Request) -> Response:
        checks = {}
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            checks["database"] = "ok"
        except Exception as exc:  # pragma: no cover - failure path
            checks["database"] = f"error: {exc.__class__.__name__}"

        healthy = all(value == "ok" for value in checks.values())
        return Response(
            {"status": "ready" if healthy else "degraded", "checks": checks},
            status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class UserScopedQuerysetMixin:
    """Scope a view's queryset to the requesting user.

    Two jobs beyond the obvious one.

    ``swagger_fake_view`` -- drf-spectacular introspects every view with an
    anonymous request to derive its schema. A queryset that filters on
    ``request.user`` explodes on ``AnonymousUser``, so schema generation gets
    an empty queryset of the right model instead.

    ``lookup_value_regex`` -- primary keys are UUIDs, so the router is told as
    much. Without it the generated OpenAPI describes every detail route's path
    parameter as an untyped string, and clients generated from that schema
    lose the type.
    """

    #: Set on the subclass; the model whose rows this view exposes.
    scoped_model = None
    lookup_value_regex = "[0-9a-fA-F-]{36}"

    def scoped_queryset(self):
        raise NotImplementedError

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.scoped_model.objects.none()
        return self.scoped_queryset()
