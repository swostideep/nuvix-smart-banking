from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from nuvix.banking.models import Institution, LinkedAccount, RecurringSeries, Transaction
from nuvix.banking.serializers import (
    CashflowProjectionSerializer,
    InstitutionSerializer,
    LinkedAccountSerializer,
    RecurringSeriesSerializer,
    TransactionSerializer,
)
from nuvix.banking.services.sync import build_projection, refresh_recurring_series
from nuvix.core.permissions import IsOwner
from nuvix.core.views import UserScopedQuerysetMixin


class InstitutionListView(ListAPIView):
    """Catalogue of linkable institutions."""

    queryset = Institution.objects.filter(is_active=True).order_by("name")
    serializer_class = InstitutionSerializer
    pagination_class = None


class LinkedAccountViewSet(UserScopedQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = LinkedAccountSerializer
    permission_classes = [IsOwner]
    filterset_fields = ["account_type", "is_primary_checking"]
    scoped_model = LinkedAccount

    def scoped_queryset(self):
        return (
            LinkedAccount.objects.alive()
            .filter(user=self.request.user)
            .select_related("institution")
        )

    def perform_destroy(self, instance: LinkedAccount) -> None:
        instance.soft_delete()

    @action(detail=True, methods=["post"])
    def sync(self, request: Request, pk: str | None = None) -> Response:
        """Mark an account as freshly synced.

        Stands in for the aggregator webhook (Plaid/MX) that would normally
        drive this; the surface the rest of the platform sees is identical.
        """

        account = self.get_object()
        account.last_synced_at = timezone.now()
        account.save(update_fields=["last_synced_at", "updated_at"])
        return Response(self.get_serializer(account).data)


class TransactionViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    UserScopedQuerysetMixin,
    viewsets.GenericViewSet,
):
    serializer_class = TransactionSerializer
    permission_classes = [IsOwner]
    filterset_fields = ["account", "category", "is_pending"]
    ordering_fields = ["posted_on", "amount"]
    scoped_model = Transaction

    def scoped_queryset(self):
        return Transaction.objects.filter(user=self.request.user)

    @extend_schema(
        request=TransactionSerializer(many=True),
        responses={201: TransactionSerializer(many=True)},
    )
    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request: Request) -> Response:
        """Ingest a batch of transactions in one round trip.

        A feed refresh delivers hundreds of rows; one request per row would
        make a sync take minutes and hammer the connection pool.
        """

        serializer = self.get_serializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class RecurringSeriesView(UserScopedQuerysetMixin, ListAPIView):
    serializer_class = RecurringSeriesSerializer
    pagination_class = None
    scoped_model = RecurringSeries

    def scoped_queryset(self):
        return RecurringSeries.objects.filter(user=self.request.user).order_by(
            "-is_income", "next_expected_on"
        )


class DetectRecurringView(APIView):
    """Re-run recurrence detection over the user's transaction history."""

    @extend_schema(request=None, responses={200: RecurringSeriesSerializer(many=True)})
    def post(self, request: Request) -> Response:
        series = refresh_recurring_series(request.user)
        return Response(RecurringSeriesSerializer(series, many=True).data)


class CashflowView(APIView):
    """Projected balance curve, trough and safe-to-spend."""

    throttle_scope = "simulation"

    @extend_schema(
        parameters=[OpenApiParameter("horizon_days", int, description="1-180, default 45")],
        responses={200: CashflowProjectionSerializer},
    )
    def get(self, request: Request) -> Response:
        horizon = min(max(int(request.query_params.get("horizon_days", 45)), 1), 180)
        projection = build_projection(request.user, horizon_days=horizon)
        return Response(CashflowProjectionSerializer(projection).data)
