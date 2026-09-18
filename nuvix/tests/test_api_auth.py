"""Authentication and the tenant-isolation boundary.

The single invariant that must never break in a consumer-finance backend: one
user can never read or write another user's financial records. These tests
assert it on every user-scoped resource rather than trusting that each view
remembered to filter.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from nuvix.accounts.models import User
from nuvix.debt.models import Debt

pytestmark = pytest.mark.django_db


class TestRegistration:
    def test_registration_returns_a_usable_token_pair(self, api):
        response = api.post(
            reverse("v1:accounts:register"),
            {
                "email": "New.User@Example.com",
                "password": "a-sufficiently-long-password",
                "password_confirm": "a-sufficiently-long-password",
                "first_name": "New",
            },
            format="json",
        )
        assert response.status_code == 201
        assert response.data["access"]
        assert response.data["refresh"]
        # Email is normalised to lower case on the way in.
        assert response.data["user"]["email"] == "new.user@example.com"

    def test_a_profile_is_created_with_the_user(self, api):
        api.post(
            reverse("v1:accounts:register"),
            {
                "email": "profiled@example.com",
                "password": "a-sufficiently-long-password",
                "password_confirm": "a-sufficiently-long-password",
            },
            format="json",
        )
        assert User.objects.get(email="profiled@example.com").profile is not None

    def test_mismatched_passwords_are_rejected(self, api):
        response = api.post(
            reverse("v1:accounts:register"),
            {
                "email": "x@example.com",
                "password": "a-sufficiently-long-password",
                "password_confirm": "something-else-entirely",
            },
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["code"] == "validation_error"

    def test_a_weak_password_is_rejected(self, api):
        response = api.post(
            reverse("v1:accounts:register"),
            {"email": "x@example.com", "password": "password", "password_confirm": "password"},
            format="json",
        )
        assert response.status_code == 400

    def test_a_duplicate_email_is_rejected(self, api, user):
        response = api.post(
            reverse("v1:accounts:register"),
            {
                "email": user.email,
                "password": "a-sufficiently-long-password",
                "password_confirm": "a-sufficiently-long-password",
            },
            format="json",
        )
        assert response.status_code == 400

    def test_email_uniqueness_is_case_insensitive(self, api, user):
        response = api.post(
            reverse("v1:accounts:register"),
            {
                "email": user.email.upper(),
                "password": "a-sufficiently-long-password",
                "password_confirm": "a-sufficiently-long-password",
            },
            format="json",
        )
        assert response.status_code == 400


class TestLogin:
    def test_valid_credentials_return_tokens_and_the_user(self, api, user):
        response = api.post(
            reverse("v1:accounts:login"),
            {"email": user.email, "password": "correct-horse-battery"},
            format="json",
        )
        assert response.status_code == 200
        assert response.data["access"]
        assert response.data["user"]["email"] == user.email

    def test_a_wrong_password_is_rejected(self, api, user):
        response = api.post(
            reverse("v1:accounts:login"),
            {"email": user.email, "password": "not-the-password"},
            format="json",
        )
        assert response.status_code == 401

    def test_a_bearer_token_authenticates_subsequent_calls(self, api, user):
        token = api.post(
            reverse("v1:accounts:login"),
            {"email": user.email, "password": "correct-horse-battery"},
            format="json",
        ).data["access"]
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        assert api.get(reverse("v1:accounts:me")).status_code == 200


class TestAuthenticationRequired:
    @pytest.mark.parametrize(
        "route",
        [
            "v1:accounts:me",
            "v1:debt:debt-list",
            "v1:debt:plan-list",
            "v1:credit:score",
            "v1:banking:cashflow",
            "v1:marketplace:offers",
            "v1:comms:messages",
        ],
    )
    def test_anonymous_access_is_refused(self, api, route):
        assert api.get(reverse(route)).status_code == 401


class TestTenantIsolation:
    """Every one of these would be a reportable incident if it failed."""

    def test_another_users_debt_is_invisible_in_the_list(self, api, user, other_user):
        Debt.objects.create(
            user=other_user,
            name="Their card",
            balance=Decimal("1000"),
            apr=Decimal("20"),
            minimum_payment=Decimal("40"),
        )
        api.force_authenticate(user=user)
        response = api.get(reverse("v1:debt:debt-list"))
        assert response.status_code == 200
        assert response.data["results"] == []

    def test_another_users_debt_cannot_be_fetched_by_id(self, api, user, other_user):
        theirs = Debt.objects.create(
            user=other_user,
            name="Their card",
            balance=Decimal("1000"),
            apr=Decimal("20"),
            minimum_payment=Decimal("40"),
        )
        api.force_authenticate(user=user)
        response = api.get(reverse("v1:debt:debt-detail", args=[theirs.id]))
        assert response.status_code == 404

    def test_another_users_debt_cannot_be_modified(self, api, user, other_user):
        theirs = Debt.objects.create(
            user=other_user,
            name="Their card",
            balance=Decimal("1000"),
            apr=Decimal("20"),
            minimum_payment=Decimal("40"),
        )
        api.force_authenticate(user=user)
        response = api.patch(
            reverse("v1:debt:debt-detail", args=[theirs.id]),
            {"balance": "1.00"},
            format="json",
        )
        assert response.status_code == 404
        theirs.refresh_from_db()
        assert theirs.balance == Decimal("1000.00")

    def test_another_users_debt_cannot_be_deleted(self, api, user, other_user):
        theirs = Debt.objects.create(
            user=other_user,
            name="Their card",
            balance=Decimal("1000"),
            apr=Decimal("20"),
            minimum_payment=Decimal("40"),
        )
        api.force_authenticate(user=user)
        assert api.delete(reverse("v1:debt:debt-detail", args=[theirs.id])).status_code == 404
        assert Debt.objects.alive().filter(pk=theirs.pk).exists()

    def test_a_payment_cannot_be_attached_to_another_users_debt(self, api, user, other_user):
        theirs = Debt.objects.create(
            user=other_user,
            name="Their card",
            balance=Decimal("1000"),
            apr=Decimal("20"),
            minimum_payment=Decimal("40"),
        )
        api.force_authenticate(user=user)
        response = api.post(
            reverse("v1:debt:payment-list"),
            {"debt": str(theirs.id), "paid_on": "2026-01-05", "amount": "50.00"},
            format="json",
        )
        assert response.status_code == 400

    def test_a_transaction_cannot_be_attached_to_another_users_account(
        self, api, user, other_user, institution
    ):
        from nuvix.banking.models import AccountType, LinkedAccount

        theirs = LinkedAccount.objects.create(
            user=other_user,
            institution=institution,
            name="Theirs",
            account_type=AccountType.CHECKING,
            mask="0001",
        )
        api.force_authenticate(user=user)
        response = api.post(
            reverse("v1:banking:transaction-list"),
            {
                "account": str(theirs.id),
                "posted_on": "2026-01-05",
                "amount": "-20.00",
                "merchant": "Nope",
            },
            format="json",
        )
        assert response.status_code == 400


class TestStaffOnlyReports:
    @pytest.mark.parametrize(
        "route",
        [
            "v1:analytics:report-channels",
            "v1:analytics:report-retention",
            "v1:analytics:report-funnel",
            "v1:analytics:report-attribution",
            "v1:analytics:report-payback",
        ],
    )
    def test_a_normal_user_cannot_read_platform_reports(self, api, user, route):
        api.force_authenticate(user=user)
        assert api.get(reverse(route)).status_code == 403

    def test_staff_can_read_platform_reports(self, api, staff_user):
        api.force_authenticate(user=staff_user)
        response = api.get(reverse("v1:analytics:report-channels"))
        assert response.status_code == 200
        assert "results" in response.data


class TestErrorEnvelope:
    def test_errors_share_one_shape(self, api, user):
        api.force_authenticate(user=user)
        response = api.post(reverse("v1:debt:debt-list"), {}, format="json")
        assert response.status_code == 400
        assert set(response.data["error"]) == {"code", "message", "details", "request_id"}

    def test_a_request_id_is_echoed_back(self, api, user):
        api.force_authenticate(user=user)
        response = api.get(reverse("v1:accounts:me"), HTTP_X_REQUEST_ID="abc-123")
        assert response["X-Request-ID"] == "abc-123"

    def test_a_request_id_is_generated_when_absent(self, api, user):
        api.force_authenticate(user=user)
        assert api.get(reverse("v1:accounts:me"))["X-Request-ID"]


class TestHealth:
    def test_liveness_needs_no_auth(self, api):
        assert api.get(reverse("health")).status_code == 200

    def test_readiness_checks_the_database(self, api):
        response = api.get(reverse("readiness"))
        assert response.status_code == 200
        assert response.data["checks"]["database"] == "ok"


class TestOwnershipPermission:
    """Regression cover for a real bug: ``permission_classes = [IsOwner]``
    replaces the default ``IsAuthenticated``, so ``IsOwner`` must enforce
    authentication itself or every collection route opens up."""

    @pytest.mark.parametrize(
        "route",
        [
            "v1:debt:debt-list",
            "v1:debt:payment-list",
            "v1:banking:linked-account-list",
            "v1:banking:transaction-list",
        ],
    )
    def test_is_owner_routes_reject_anonymous_callers(self, api, route):
        response = api.get(reverse(route))
        assert response.status_code == 401, (
            "IsOwner must implement has_permission; a 400 here means an "
            "anonymous request reached the queryset."
        )

    def test_is_owner_grants_access_to_your_own_record(self, api, user):
        debt = Debt.objects.create(
            user=user,
            name="Mine",
            balance=Decimal("500"),
            apr=Decimal("15"),
            minimum_payment=Decimal("25"),
        )
        api.force_authenticate(user=user)
        assert api.get(reverse("v1:debt:debt-detail", args=[debt.id])).status_code == 200
