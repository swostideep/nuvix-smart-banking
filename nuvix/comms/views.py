from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import generics
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from nuvix.comms.models import CommunicationRule, Message
from nuvix.comms.serializers import (
    CommunicationRuleSerializer,
    MessageSerializer,
    SweepResultSerializer,
)
from nuvix.comms.services.dispatcher import sweep_user, weekly_bucket
from nuvix.comms.services.ratelimit import peek
from nuvix.core.permissions import IsStaffOrReadOnly
from nuvix.core.views import UserScopedQuerysetMixin


class MessageListView(UserScopedQuerysetMixin, generics.ListAPIView):
    """The user's own inbox, including messages that were suppressed."""

    serializer_class = MessageSerializer
    filterset_fields = ["status", "channel"]
    scoped_model = Message

    def scoped_queryset(self):
        return Message.objects.filter(user=self.request.user).select_related("rule")


class RuleListView(generics.ListAPIView):
    serializer_class = CommunicationRuleSerializer
    permission_classes = [IsStaffOrReadOnly]
    pagination_class = None

    def get_queryset(self):
        return CommunicationRule.objects.select_related("template").all()


class SweepView(APIView):
    """Run the communications pipeline for the calling user.

    Exposed so that the behaviour is inspectable without waiting for the
    nightly beat schedule -- the same function the Celery task calls.
    """

    @extend_schema(request=None, responses={200: SweepResultSerializer})
    def post(self, request: Request) -> Response:
        stats = sweep_user(request.user)
        stats["remaining_message_budget"] = round(
            peek("comms", str(request.user.id), weekly_bucket()), 2
        )
        return Response(stats)
