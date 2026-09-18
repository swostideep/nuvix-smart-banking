from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from nuvix.accounts.models import Profile
from nuvix.accounts.serializers import (
    ChangePasswordSerializer,
    NuvixTokenObtainPairSerializer,
    ProfileSerializer,
    RegistrationSerializer,
    UserSerializer,
)


class RegistrationView(generics.CreateAPIView):
    """Create an account and return a usable token pair immediately."""

    serializer_class = RegistrationSerializer
    permission_classes = [AllowAny]
    throttle_scope = "auth"


class LoginView(TokenObtainPairView):
    serializer_class = NuvixTokenObtainPairSerializer
    permission_classes = [AllowAny]
    throttle_scope = "auth"


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class ProfileView(generics.RetrieveUpdateAPIView):
    """The financial profile. Completing it flips ``onboarding_completed_at``."""

    serializer_class = ProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self) -> Profile:
        profile, _ = Profile.objects.get_or_create(user=self.request.user)
        return profile

    def perform_update(self, serializer: ProfileSerializer) -> None:
        profile = serializer.save()
        required = profile.monthly_net_income > 0 and profile.annual_income > 0
        if required and profile.onboarding_completed_at is None:
            profile.onboarding_completed_at = timezone.now()
            profile.save(update_fields=["onboarding_completed_at"])


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth"

    @extend_schema(request=ChangePasswordSerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_204_NO_CONTENT)
