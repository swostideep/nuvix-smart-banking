from __future__ import annotations

from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from nuvix.accounts.models import Profile, User


class ProfileSerializer(serializers.ModelSerializer):
    age = serializers.IntegerField(read_only=True)
    monthly_gross_income = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )
    monthly_surplus = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Profile
        fields = (
            "date_of_birth",
            "state",
            "annual_income",
            "monthly_net_income",
            "pay_frequency",
            "employment_status",
            "months_employed",
            "dependents",
            "has_mortgage",
            "monthly_fixed_expenses",
            "onboarding_completed_at",
            "age",
            "monthly_gross_income",
            "monthly_surplus",
        )
        read_only_fields = ("onboarding_completed_at",)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        net = attrs.get("monthly_net_income", getattr(self.instance, "monthly_net_income", 0))
        annual = attrs.get("annual_income", getattr(self.instance, "annual_income", 0))
        if annual and net and net * 12 > annual * 2:
            raise serializers.ValidationError(
                {"monthly_net_income": "Net income is implausible against annual income."}
            )
        return attrs


class UserSerializer(serializers.ModelSerializer):
    profile = ProfileSerializer(read_only=True)
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "phone",
            "full_name",
            "date_joined",
            "profile",
        )
        read_only_fields = ("id", "date_joined")


class RegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    password_confirm = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ("email", "password", "password_confirm", "first_name", "last_name", "phone")

    def validate_email(self, value: str) -> str:
        normalised = value.strip().lower()
        if User.objects.filter(email=normalised).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return normalised

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        validate_password(attrs["password"])
        return attrs

    @transaction.atomic
    def create(self, validated_data: dict[str, Any]) -> User:
        password = validated_data.pop("password")
        return User.objects.create_user(password=password, **validated_data)

    def to_representation(self, instance: User) -> dict[str, Any]:
        refresh = RefreshToken.for_user(instance)
        return {
            "user": UserSerializer(instance).data,
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }


class NuvixTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Return the user alongside the token pair.

    Saves the mobile client a second round trip on every cold start, which is
    the difference between a login that feels instant and one that does not.
    """

    @classmethod
    def get_token(cls, user: User) -> RefreshToken:
        token = super().get_token(user)
        token["email"] = user.email
        return token

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value: str) -> str:
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value: str) -> str:
        validate_password(value, self.context["request"].user)
        return value

    def save(self, **kwargs: Any) -> User:
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password"])
        return user
