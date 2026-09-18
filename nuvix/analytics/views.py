"""Reporting endpoints.

Every report is staff-only. These queries aggregate across all users, which
makes them a very different security proposition from the rest of the API --
a bug that leaks one user's balance is bad; one that leaks the whole funnel is
a different category of incident.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from nuvix.analytics import queries
from nuvix.analytics.models import FunnelEvent, MarketingSpend, Subscription, TouchPoint
from nuvix.analytics.serializers import (
    AttributionSerializer,
    ChannelPerformanceSerializer,
    DateWindowSerializer,
    FunnelEventSerializer,
    FunnelStepSerializer,
    MarketingSpendSerializer,
    SubscriptionSerializer,
    TouchPointSerializer,
)
from nuvix.core.views import UserScopedQuerysetMixin

_WINDOW_PARAMS = [
    OpenApiParameter("start", str, description="ISO date, inclusive"),
    OpenApiParameter("end", str, description="ISO date, exclusive"),
]


class _ReportView(APIView):
    """Shared window parsing and the staff-only permission."""

    permission_classes = [IsAdminUser]
    throttle_scope = "analytics"

    def window(self, request: Request) -> tuple:
        serializer = DateWindowSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data["start"], serializer.validated_data["end"]


class ChannelPerformanceView(_ReportView):
    @extend_schema(
        parameters=_WINDOW_PARAMS, responses={200: ChannelPerformanceSerializer(many=True)}
    )
    def get(self, request: Request) -> Response:
        start, end = self.window(request)
        rows = queries.channel_performance(start, end)
        return Response({"start": start, "end": end, "results": rows})


class CohortRetentionView(_ReportView):
    @extend_schema(
        parameters=[OpenApiParameter("months", int, description="1-36, default 12")],
        responses={200: dict},
    )
    def get(self, request: Request) -> Response:
        months = min(max(int(request.query_params.get("months", 12)), 1), 36)
        return Response({"months": months, "results": queries.cohort_retention(months)})


class FunnelView(_ReportView):
    @extend_schema(
        parameters=[*_WINDOW_PARAMS, OpenApiParameter("channel", str)],
        responses={200: FunnelStepSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        start, end = self.window(request)
        channel = request.query_params.get("channel") or None
        rows = queries.funnel_conversion(start, end, channel)
        return Response({"start": start, "end": end, "channel": channel, "results": rows})


class AttributionView(_ReportView):
    @extend_schema(parameters=_WINDOW_PARAMS, responses={200: AttributionSerializer(many=True)})
    def get(self, request: Request) -> Response:
        start, end = self.window(request)
        return Response(
            {"start": start, "end": end, "results": queries.attribution_comparison(start, end)}
        )


class CampaignPaybackView(_ReportView):
    @extend_schema(parameters=_WINDOW_PARAMS, responses={200: dict})
    def get(self, request: Request) -> Response:
        start, end = self.window(request)
        return Response(
            {"start": start, "end": end, "results": queries.campaign_payback(start, end)}
        )


class TouchPointCreateView(generics.CreateAPIView):
    """Ingest a marketing touch.

    Authenticated because an open endpoint that writes rows keyed by an
    attacker-supplied ``anonymous_id`` is an attribution-poisoning primitive.
    """

    serializer_class = TouchPointSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return TouchPoint.objects.none()


class FunnelEventCreateView(generics.CreateAPIView):
    serializer_class = FunnelEventSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return FunnelEvent.objects.none()


class MarketingSpendViewSet(generics.ListCreateAPIView):
    serializer_class = MarketingSpendSerializer
    permission_classes = [IsAdminUser]
    filterset_fields = ["channel", "campaign"]

    def get_queryset(self):
        return MarketingSpend.objects.all().order_by("-spend_on")


class SubscriptionView(UserScopedQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = SubscriptionSerializer
    scoped_model = Subscription

    def scoped_queryset(self):
        return Subscription.objects.filter(user=self.request.user)

    def perform_create(self, serializer: SubscriptionSerializer) -> None:
        serializer.save(user=self.request.user)
