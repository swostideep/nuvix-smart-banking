from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from nuvix.core.permissions import IsOwner
from nuvix.core.views import UserScopedQuerysetMixin
from nuvix.debt.models import Debt, PaymentRecord, PayoffPlan
from nuvix.debt.serializers import (
    DebtSerializer,
    PaymentRecordSerializer,
    PayoffPlanSerializer,
    PayoffResultSerializer,
    PlanRequestSerializer,
    TargetDateRequestSerializer,
)
from nuvix.debt.services import planner
from nuvix.debt.services.strategies import budget_for_target_months


class DebtViewSet(UserScopedQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = DebtSerializer
    permission_classes = [IsOwner]
    filterset_fields = ["kind"]
    ordering_fields = ["balance", "apr", "created_at"]
    scoped_model = Debt

    def scoped_queryset(self):
        return Debt.objects.alive().filter(user=self.request.user)

    def perform_destroy(self, instance: Debt) -> None:
        instance.soft_delete()


class PaymentRecordViewSet(UserScopedQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = PaymentRecordSerializer
    permission_classes = [IsOwner]
    filterset_fields = ["debt"]
    scoped_model = PaymentRecord

    def scoped_queryset(self):
        return PaymentRecord.objects.filter(user=self.request.user).select_related("debt")


class SimulatePlanView(APIView):
    """Project a plan without storing it.

    The endpoint the "what if I paid $50 more" slider calls, so it is
    throttled separately and never writes.
    """

    throttle_scope = "simulation"

    @extend_schema(request=PlanRequestSerializer, responses={200: PayoffResultSerializer})
    def post(self, request: Request) -> Response:
        payload = PlanRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        result = planner.build(
            request.user,
            strategy=payload.validated_data["strategy"],
            monthly_budget=payload.validated_data.get("monthly_budget"),
            keep_schedule=payload.validated_data["include_schedule"],
        )
        data = PayoffResultSerializer(result).data
        if not payload.validated_data["include_schedule"]:
            data.pop("schedule", None)
        return Response(data)


class CompareStrategiesView(APIView):
    """Score every strategy side by side against the minimum-only baseline."""

    throttle_scope = "simulation"

    @extend_schema(request=PlanRequestSerializer, responses={200: dict})
    def post(self, request: Request) -> Response:
        payload = PlanRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        results = planner.compare(
            request.user, monthly_budget=payload.validated_data.get("monthly_budget")
        )
        return Response(
            {
                name: {
                    **PayoffResultSerializer(result).data,
                    "schedule": None,
                }
                for name, result in results.items()
            }
        )


class CreatePlanView(APIView):
    """Compute a plan and make it the user's active one."""

    @extend_schema(request=PlanRequestSerializer, responses={201: PayoffPlanSerializer})
    def post(self, request: Request) -> Response:
        payload = PlanRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        plan, _ = planner.save_plan(
            request.user,
            strategy=payload.validated_data["strategy"],
            monthly_budget=payload.validated_data.get("monthly_budget"),
        )
        return Response(PayoffPlanSerializer(plan).data, status=status.HTTP_201_CREATED)


class PlanListView(UserScopedQuerysetMixin, ListAPIView):
    serializer_class = PayoffPlanSerializer
    scoped_model = PayoffPlan

    def scoped_queryset(self):
        return PayoffPlan.objects.filter(user=self.request.user)


class ActivePlanView(APIView):
    @extend_schema(responses={200: PayoffPlanSerializer, 404: dict})
    def get(self, request: Request) -> Response:
        plan = PayoffPlan.objects.filter(user=request.user, is_active=True).first()
        if plan is None:
            return Response(
                {
                    "error": {
                        "code": "no_active_plan",
                        "message": "No active plan.",
                        "details": {},
                        "request_id": getattr(request, "request_id", None),
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(PayoffPlanSerializer(plan).data)


class BudgetForTargetView(APIView):
    """Reverse the question: what budget clears this debt by month N?"""

    throttle_scope = "simulation"

    @extend_schema(request=TargetDateRequestSerializer, responses={200: dict})
    def post(self, request: Request) -> Response:
        payload = TargetDateRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        debts = planner.require_debts(request.user)
        budget = budget_for_target_months(
            debts,
            target_months=payload.validated_data["target_months"],
            strategy=payload.validated_data["strategy"],
        )
        return Response(
            {
                "target_months": payload.validated_data["target_months"],
                "strategy": payload.validated_data["strategy"],
                "required_monthly_budget": str(budget),
            }
        )
