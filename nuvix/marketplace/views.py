from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from nuvix.core.permissions import IsStaffOrReadOnly
from nuvix.core.views import UserScopedQuerysetMixin
from nuvix.marketplace.models import Offer, Product
from nuvix.marketplace.serializers import (
    MatchedOfferSerializer,
    MatchRequestSerializer,
    OfferSerializer,
    ProductSerializer,
)
from nuvix.marketplace.services import matching


class ProductListView(generics.ListAPIView):
    """The full partner catalogue, unfiltered by the user's eligibility."""

    serializer_class = ProductSerializer
    permission_classes = [IsStaffOrReadOnly]
    filterset_fields = ["product_type", "partner"]
    ordering_fields = ["apr_min", "min_credit_score"]

    def get_queryset(self):
        return Product.objects.filter(is_active=True).select_related("partner")


class MatchView(APIView):
    """Rank the catalogue for this user by expected benefit."""

    throttle_scope = "simulation"

    @extend_schema(
        request=MatchRequestSerializer, responses={200: MatchedOfferSerializer(many=True)}
    )
    def post(self, request: Request) -> Response:
        payload = MatchRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        scored = matching.match(
            request.user,
            product_type=payload.validated_data.get("product_type"),
            limit=payload.validated_data["limit"],
        )
        if payload.validated_data["persist"]:
            matching.persist_offers(request.user, scored)

        return Response(
            {
                "count": len(scored),
                "results": MatchedOfferSerializer(scored, many=True).data,
            }
        )


class OfferListView(UserScopedQuerysetMixin, generics.ListAPIView):
    serializer_class = OfferSerializer
    scoped_model = Offer

    def scoped_queryset(self):
        return (
            Offer.objects.filter(user=self.request.user)
            .select_related("product", "product__partner")
            .order_by("-rank_score")
        )


class OfferClickView(APIView):
    """Record that a user clicked through to a partner."""

    @extend_schema(request=None, responses={200: OfferSerializer, 404: dict})
    def post(self, request: Request, pk: str) -> Response:
        offer = Offer.objects.filter(user=request.user, pk=pk).first()
        if offer is None:
            return Response(
                {
                    "error": {
                        "code": "not_found",
                        "message": "Offer not found.",
                        "details": {},
                        "request_id": getattr(request, "request_id", None),
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        offer.clicked_at = timezone.now()
        offer.save(update_fields=["clicked_at", "updated_at"])
        return Response(OfferSerializer(offer).data)
