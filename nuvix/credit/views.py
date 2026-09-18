from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from nuvix.core.views import UserScopedQuerysetMixin
from nuvix.credit.engine.simulator import rank_actions, simulate
from nuvix.credit.models import CreditProfile, RiskAssessment
from nuvix.credit.serializers import (
    CreditProfileSerializer,
    RiskAssessmentSerializer,
    RiskRequestSerializer,
    ScoreBreakdownSerializer,
    ScoreSnapshotSerializer,
    SimulationRequestSerializer,
    SimulationResultSerializer,
)
from nuvix.credit.services import risk, scoring


class CreditProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = CreditProfileSerializer

    def get_object(self) -> CreditProfile:
        return scoring.get_or_create_profile(self.request.user)

    def perform_update(self, serializer: CreditProfileSerializer) -> None:
        serializer.save()
        scoring.rescore(self.request.user)


class ScoreView(APIView):
    """Current score with its full factor breakdown."""

    @extend_schema(responses={200: ScoreBreakdownSerializer})
    def get(self, request: Request) -> Response:
        breakdown = scoring.rescore(request.user)
        payload = ScoreBreakdownSerializer(breakdown).data
        opportunity = breakdown.biggest_opportunity
        payload["biggest_opportunity"] = (
            {
                "key": opportunity.key,
                "label": opportunity.label,
                "points_available": opportunity.points_available,
                "reason": opportunity.reason,
            }
            if opportunity
            else None
        )
        return Response(payload)


class ScoreHistoryView(APIView):
    @extend_schema(responses={200: ScoreSnapshotSerializer(many=True)})
    def get(self, request: Request) -> Response:
        days = min(max(int(request.query_params.get("days", 365)), 7), 1825)
        history = scoring.score_history(request.user, days=days)
        return Response(ScoreSnapshotSerializer(history, many=True).data)


class RefreshFromAccountsView(APIView):
    """Recompute utilisation and mix from linked accounts, then rescore."""

    @extend_schema(request=None, responses={200: ScoreBreakdownSerializer})
    def post(self, request: Request) -> Response:
        scoring.derive_from_linked_accounts(request.user)
        return Response(ScoreBreakdownSerializer(scoring.rescore(request.user)).data)


class SimulateScoreView(APIView):
    """Project the score impact of one action."""

    throttle_scope = "simulation"

    @extend_schema(
        request=SimulationRequestSerializer, responses={200: SimulationResultSerializer}
    )
    def post(self, request: Request) -> Response:
        payload = SimulationRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        action, kwargs = payload.engine_kwargs()
        profile = scoring.get_or_create_profile(request.user)
        result = simulate(profile.to_inputs(), action, **kwargs)
        return Response(SimulationResultSerializer(result).data)


class RecommendedActionsView(APIView):
    """Rank realistic next actions by the points they would actually earn."""

    throttle_scope = "simulation"

    @extend_schema(responses={200: SimulationResultSerializer(many=True)})
    def get(self, request: Request) -> Response:
        profile = scoring.get_or_create_profile(request.user)
        candidates = scoring.default_action_candidates(profile)
        results = rank_actions(profile.to_inputs(), candidates)
        return Response(SimulationResultSerializer(results, many=True).data)


class AssessRiskView(APIView):
    """Probability of default for a proposed borrowing."""

    @extend_schema(request=RiskRequestSerializer, responses={201: RiskAssessmentSerializer})
    def post(self, request: Request) -> Response:
        payload = RiskRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        assessment = risk.assess(request.user, **payload.validated_data)
        return Response(
            RiskAssessmentSerializer(assessment).data, status=status.HTTP_201_CREATED
        )


class RiskAssessmentListView(UserScopedQuerysetMixin, generics.ListAPIView):
    serializer_class = RiskAssessmentSerializer
    scoped_model = RiskAssessment

    def scoped_queryset(self):
        return RiskAssessment.objects.filter(user=self.request.user)
