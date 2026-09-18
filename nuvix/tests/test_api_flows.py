"""End-to-end flows through the API.

Each test walks a real user journey rather than poking one endpoint, because
the failures that matter are usually at the seams: a plan built from debts
created through a different endpoint, a score that moves after an account is
linked, an offer priced from a credit profile written elsewhere.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from nuvix.debt.models import PayoffPlan
from nuvix.marketplace.models import Offer

pytestmark = pytest.mark.django_db


class TestDebtJourney:
    def test_create_debts_then_simulate_compare_and_save_a_plan(self, auth, user):
        for payload in (
            {
                "name": "Visa",
                "kind": "credit_card",
                "balance": "4200.00",
                "apr": "24.99",
                "minimum_payment": "95.00",
            },
            {
                "name": "Store card",
                "kind": "credit_card",
                "balance": "800.00",
                "apr": "28.99",
                "minimum_payment": "35.00",
            },
        ):
            assert (
                auth.post(reverse("v1:debt:debt-list"), payload, format="json").status_code
                == 201
            )

        simulated = auth.post(
            reverse("v1:debt:plan-simulate"),
            {"strategy": "optimal", "monthly_budget": "600.00"},
            format="json",
        )
        assert simulated.status_code == 200
        assert simulated.data["months_to_debt_free"] > 0
        assert Decimal(simulated.data["total_interest"]) > 0
        # A simulation must not persist anything.
        assert PayoffPlan.objects.count() == 0

        compared = auth.post(
            reverse("v1:debt:plan-compare"),
            {"monthly_budget": "600.00"},
            format="json",
        )
        assert compared.status_code == 200
        assert set(compared.data) == {"avalanche", "snowball", "optimal", "minimum_only"}

        created = auth.post(
            reverse("v1:debt:plan-create"),
            {"strategy": "optimal", "monthly_budget": "600.00"},
            format="json",
        )
        assert created.status_code == 201
        assert PayoffPlan.objects.filter(user=user, is_active=True).count() == 1

        active = auth.get(reverse("v1:debt:plan-active"))
        assert active.status_code == 200
        assert active.data["strategy"] == "optimal"

    def test_a_schedule_is_returned_only_when_asked_for(self, auth, debts):
        without = auth.post(
            reverse("v1:debt:plan-simulate"),
            {"strategy": "avalanche", "monthly_budget": "700.00"},
            format="json",
        )
        assert "schedule" not in without.data

        with_schedule = auth.post(
            reverse("v1:debt:plan-simulate"),
            {"strategy": "avalanche", "monthly_budget": "700.00", "include_schedule": True},
            format="json",
        )
        assert len(with_schedule.data["schedule"]) == with_schedule.data["months_to_debt_free"]

    def test_only_one_plan_is_active_at_a_time(self, auth, user, debts):
        for strategy in ("avalanche", "snowball", "optimal"):
            auth.post(
                reverse("v1:debt:plan-create"),
                {"strategy": strategy, "monthly_budget": "700.00"},
                format="json",
            )
        assert PayoffPlan.objects.filter(user=user, is_active=True).count() == 1
        assert PayoffPlan.objects.filter(user=user).count() == 3

    def test_an_underfunded_budget_returns_a_domain_error(self, auth, debts):
        response = auth.post(
            reverse("v1:debt:plan-simulate"),
            {"strategy": "avalanche", "monthly_budget": "100.00"},
            format="json",
        )
        assert response.status_code == 422
        assert response.data["error"]["code"] == "insufficient_budget"
        assert "minimum_required" in response.data["error"]["details"]

    def test_planning_with_no_debts_is_a_clear_error(self, auth):
        response = auth.post(
            reverse("v1:debt:plan-simulate"), {"strategy": "optimal"}, format="json"
        )
        assert response.status_code == 422
        assert response.data["error"]["code"] == "no_debts"

    def test_budget_for_a_target_date(self, auth, debts):
        response = auth.post(
            reverse("v1:debt:plan-budget-target"),
            {"target_months": 18, "strategy": "avalanche"},
            format="json",
        )
        assert response.status_code == 200
        assert Decimal(response.data["required_monthly_budget"]) > 0

    def test_deleting_a_debt_archives_rather_than_destroys(self, auth, user, debts):
        from nuvix.debt.models import Debt

        target = debts[0]
        assert auth.delete(reverse("v1:debt:debt-detail", args=[target.id])).status_code == 204
        assert not Debt.objects.alive().filter(pk=target.pk).exists()
        assert Debt.objects.filter(pk=target.pk).exists()


class TestCreditJourney:
    def test_score_breakdown_names_the_biggest_opportunity(self, auth, credit_profile):
        response = auth.get(reverse("v1:credit:score"))
        assert response.status_code == 200
        assert 300 <= response.data["score"] <= 850
        assert len(response.data["factors"]) == 5
        assert response.data["biggest_opportunity"]["points_available"] > 0

    def test_simulating_a_paydown_reports_a_gain(self, auth, credit_profile):
        response = auth.post(
            reverse("v1:credit:score-simulate"),
            {"action": "pay_down_balance", "amount": "2500.00"},
            format="json",
        )
        assert response.status_code == 200
        assert response.data["score_change"] > 0
        assert response.data["explanation"]

    def test_a_simulation_missing_its_parameter_is_rejected(self, auth, credit_profile):
        response = auth.post(
            reverse("v1:credit:score-simulate"), {"action": "pay_down_balance"}, format="json"
        )
        assert response.status_code == 400

    def test_recommendations_are_ranked(self, auth, credit_profile):
        response = auth.get(reverse("v1:credit:score-recommendations"))
        assert response.status_code == 200
        changes = [r["score_change"] for r in response.data]
        assert changes == sorted(changes, reverse=True)

    def test_history_records_a_movement(self, auth, credit_profile):
        auth.get(reverse("v1:credit:score"))
        credit_profile.total_balance = Decimal("400.00")
        credit_profile.save()
        auth.get(reverse("v1:credit:score"))

        history = auth.get(reverse("v1:credit:score-history"))
        assert history.status_code == 200
        assert len(history.data) >= 1

    def test_refreshing_from_linked_accounts_rescan(self, auth, credit_profile, card):
        response = auth.post(reverse("v1:credit:score-refresh"))
        assert response.status_code == 200
        assert 300 <= response.data["score"] <= 850

    def test_a_risk_assessment_is_recorded(self, auth, credit_profile):
        response = auth.post(
            reverse("v1:credit:risk-assess"),
            {
                "loan_amount": "12000.00",
                "interest_rate": "13.50",
                "loan_term_months": 48,
                "loan_purpose": "debt_consolidation",
            },
            format="json",
        )
        assert response.status_code == 201
        assert response.data["decision"] in {"approve", "refer", "decline"}
        assert response.data["risk_grade"] in set("ABCDEF")

        listed = auth.get(reverse("v1:credit:risk-list"))
        assert listed.data["results"][0]["id"] == response.data["id"]


class TestBankingJourney:
    def test_link_ingest_detect_and_project(self, auth, user, checking, institution):
        import datetime as dt

        rows = []
        for index in range(8):
            rows.append(
                {
                    "account": str(checking.id),
                    "posted_on": (
                        dt.date(2026, 1, 2) + dt.timedelta(days=14 * index)
                    ).isoformat(),
                    "amount": "2050.00",
                    "merchant": "ACME PAYROLL DIRECT DEP",
                    "category": "income",
                }
            )
        for index in range(6):
            rows.append(
                {
                    "account": str(checking.id),
                    "posted_on": (
                        dt.date(2026, 1, 3) + dt.timedelta(days=30 * index)
                    ).isoformat(),
                    "amount": "-1450.00",
                    "merchant": "GREENLEAF PROPERTY MGMT",
                    "category": "housing",
                }
            )

        created = auth.post(reverse("v1:banking:transaction-bulk"), rows, format="json")
        assert created.status_code == 201

        detected = auth.post(reverse("v1:banking:recurring-detect"))
        assert detected.status_code == 200
        labels = {row["label"] for row in detected.data}
        assert any("PAYROLL" in label or "ACME" in label for label in labels)
        assert any(row["is_income"] for row in detected.data)

        cashflow = auth.get(reverse("v1:banking:cashflow"), {"horizon_days": 60})
        assert cashflow.status_code == 200
        assert cashflow.data["events"]
        assert Decimal(cashflow.data["safe_to_spend"]) >= 0

    def test_a_credit_card_requires_a_limit(self, auth, institution):
        response = auth.post(
            reverse("v1:banking:linked-account-list"),
            {
                "institution": str(institution.id),
                "name": "Card",
                "account_type": "credit_card",
                "mask": "1234",
            },
            format="json",
        )
        assert response.status_code == 400
        assert "credit_limit" in response.data["error"]["details"]


class TestMarketplaceJourney:
    def test_matching_ranks_and_optionally_persists(
        self, auth, user, credit_profile, debts, products
    ):
        credit_profile.score = 750
        credit_profile.save()

        response = auth.post(
            reverse("v1:marketplace:match"), {"limit": 5, "persist": True}, format="json"
        )
        assert response.status_code == 200
        assert response.data["count"] >= 1

        scores = [Decimal(r["rank_score"]) for r in response.data["results"]]
        assert scores == sorted(scores, reverse=True)
        assert Offer.objects.filter(user=user).count() == response.data["count"]

    def test_clicking_an_offer_is_recorded(self, auth, user, credit_profile, debts, products):
        credit_profile.score = 750
        credit_profile.save()
        auth.post(reverse("v1:marketplace:match"), {"persist": True}, format="json")

        offer = Offer.objects.filter(user=user).first()
        response = auth.post(reverse("v1:marketplace:offer-click", args=[offer.id]))
        assert response.status_code == 200
        offer.refresh_from_db()
        assert offer.clicked_at is not None

    def test_another_users_offer_cannot_be_clicked(self, api, user, other_user, products):
        offer = Offer.objects.create(
            user=other_user,
            product=products[0],
            estimated_apr=Decimal("12"),
            estimated_amount=Decimal("5000"),
            estimated_term_months=36,
            approval_odds=Decimal("0.5"),
        )
        api.force_authenticate(user=user)
        assert (
            api.post(reverse("v1:marketplace:offer-click", args=[offer.id])).status_code == 404
        )


class TestCommsJourney:
    def test_a_sweep_produces_an_inbox(self, auth, user, credit_profile):
        from nuvix.comms.models import Channel, CommunicationRule, MessageTemplate, TriggerType

        template = MessageTemplate.objects.create(
            key="util",
            channel=Channel.PUSH,
            subject="Utilisation {utilization_pct}%",
            body="Pay ${paydown_amount} for {points_gain} points.",
        )
        CommunicationRule.objects.create(
            key="util", trigger=TriggerType.UTILIZATION_HIGH, template=template, priority=90
        )

        swept = auth.post(reverse("v1:comms:sweep"))
        assert swept.status_code == 200
        assert swept.data["scheduled"] == 1
        assert "remaining_message_budget" in swept.data

        inbox = auth.get(reverse("v1:comms:messages"))
        assert inbox.status_code == 200
        assert len(inbox.data["results"]) == 1
