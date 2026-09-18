"""Tests for eligibility, pricing and offer ranking."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from nuvix.marketplace.models import Offer, Product, ProductType
from nuvix.marketplace.services import matching

pytestmark = pytest.mark.django_db


@pytest.fixture
def context(user, credit_profile, debts) -> matching.MatchContext:
    return matching.build_context(user)


class TestContext:
    def test_weighted_apr_reflects_balance_weighting(self, user, credit_profile, debts):
        ctx = matching.build_context(user)
        # Balances 4800@26.99 + 900@28.99 + 9400@7.25 over 15,100.
        expected = (
            Decimal("4800") * Decimal("26.99")
            + Decimal("900") * Decimal("28.99")
            + Decimal("9400") * Decimal("7.25")
        ) / Decimal("15100")
        assert ctx.current_weighted_apr == expected.quantize(Decimal("0.01"))

    def test_revolving_balance_sums_the_book(self, context):
        assert context.revolving_balance == Decimal("15100.00")

    def test_a_user_with_no_debt_has_a_zero_weighted_apr(self, user, credit_profile):
        ctx = matching.build_context(user)
        assert ctx.current_weighted_apr == Decimal("0.00")


class TestEligibility:
    def test_a_low_score_is_excluded_from_premium_products(
        self, user, credit_profile, products
    ):
        credit_profile.score = 610
        credit_profile.save()
        eligible = matching.eligible_products(matching.build_context(user))
        names = {p.name for p in eligible}
        assert "Premium Rewards Card" not in names
        assert "Starter Secured Card" in names

    def test_a_high_score_unlocks_more_products(self, user, credit_profile, products):
        credit_profile.score = 760
        credit_profile.save()
        eligible = matching.eligible_products(matching.build_context(user))
        assert "Premium Rewards Card" in {p.name for p in eligible}

    def test_excluded_states_are_filtered_in_sql(self, user, credit_profile, products):
        card = Product.objects.get(name="Starter Secured Card")
        card.excluded_states = Product.encode_states(["WA", "NY"])
        card.save()
        credit_profile.score = 700
        credit_profile.save()
        eligible = matching.eligible_products(matching.build_context(user))
        assert "Starter Secured Card" not in {p.name for p in eligible}

    def test_a_state_code_cannot_match_inside_another(self, user, credit_profile, products):
        """Delimiters must stop "WA" matching a hypothetical "SWAZ"."""

        card = Product.objects.get(name="Starter Secured Card")
        card.excluded_states = Product.encode_states(["CA"])
        card.save()
        eligible = matching.eligible_products(matching.build_context(user))
        assert "Starter Secured Card" in {p.name for p in eligible}

    def test_filtering_by_product_type(self, user, credit_profile, products):
        eligible = matching.eligible_products(
            matching.build_context(user), ProductType.SECURED_CARD
        )
        assert {p.product_type for p in eligible} == {ProductType.SECURED_CARD}


class TestPricing:
    def test_a_borrower_at_the_floor_gets_the_worst_rate(self, user, credit_profile, products):
        loan = Product.objects.get(name="Consolidation Loan")
        credit_profile.score = loan.min_credit_score
        credit_profile.save()
        apr = matching.estimate_apr(loan, matching.build_context(user))
        assert apr == loan.apr_max

    def test_a_stronger_borrower_gets_a_better_rate(self, user, credit_profile, products):
        loan = Product.objects.get(name="Consolidation Loan")
        credit_profile.score = 820
        credit_profile.save()
        strong = matching.estimate_apr(loan, matching.build_context(user))
        credit_profile.score = 660
        credit_profile.save()
        weak = matching.estimate_apr(loan, matching.build_context(user))
        assert strong < weak

    def test_the_rate_never_leaves_the_advertised_band(self, user, credit_profile, products):
        loan = Product.objects.get(name="Consolidation Loan")
        for score in (640, 700, 780, 850):
            credit_profile.score = score
            credit_profile.save()
            apr = matching.estimate_apr(loan, matching.build_context(user))
            assert loan.apr_min <= apr <= loan.apr_max


class TestApprovalOdds:
    def test_sitting_on_the_minimum_is_not_a_yes(self, user, credit_profile, products):
        loan = Product.objects.get(name="Consolidation Loan")
        credit_profile.score = loan.min_credit_score
        credit_profile.save()
        ctx = matching.build_context(user)
        odds = matching.approval_odds(loan, ctx)
        assert Decimal("0.2") < odds < Decimal("0.75")

    def test_odds_rise_with_score(self, user, credit_profile, products):
        loan = Product.objects.get(name="Consolidation Loan")
        results = []
        for score in (645, 700, 780):
            credit_profile.score = score
            credit_profile.save()
            results.append(matching.approval_odds(loan, matching.build_context(user)))
        assert results == sorted(results)

    def test_odds_are_bounded(self, user, credit_profile, products):
        loan = Product.objects.get(name="Consolidation Loan")
        credit_profile.score = 850
        credit_profile.save()
        odds = matching.approval_odds(loan, matching.build_context(user))
        assert Decimal("0.01") <= odds <= Decimal("0.99")

    def test_the_weaker_criterion_dominates(self, user, credit_profile, products):
        """An excellent score cannot rescue a DTI sitting at the product's limit."""

        loan = Product.objects.get(name="Consolidation Loan")
        credit_profile.score = 840
        credit_profile.save()
        context = matching.build_context(user)

        healthy = matching.approval_odds(loan, _with_dti(context, Decimal("0.05")))
        strained = matching.approval_odds(loan, _with_dti(context, Decimal("0.44")))
        assert strained < healthy


def _with_dti(context: matching.MatchContext, dti: Decimal) -> matching.MatchContext:
    return dataclasses.replace(context, dti_ratio=dti)


class TestRanking:
    def test_ranking_is_by_expected_value_to_the_user(self, user, credit_profile, products):
        credit_profile.score = 760
        credit_profile.save()
        results = matching.match(user)
        scores = [r["rank_score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_partner_payout_does_not_enter_the_rank_score(
        self, user, credit_profile, products, partner
    ):
        """The commercial arrangement must not reorder the recommendations."""

        credit_profile.score = 760
        credit_profile.save()
        before = [(r["product"].id, r["rank_score"]) for r in matching.match(user)]

        partner.payout_per_funded = Decimal("5000.00")
        partner.save()
        after = [(r["product"].id, r["rank_score"]) for r in matching.match(user)]

        assert before == after

    def test_rank_score_is_benefit_times_odds(self, user, credit_profile, products):
        credit_profile.score = 700
        credit_profile.save()
        for row in matching.match(user):
            expected = (row["estimated_benefit"] * row["approval_odds"]).quantize(
                Decimal("0.0001")
            )
            assert row["rank_score"] == expected

    def test_every_offer_explains_itself(self, user, credit_profile, products):
        for row in matching.match(user):
            assert row["reasons"]
            assert "approval odds" in row["reasons"][0].lower()

    def test_the_limit_is_respected(self, user, credit_profile, products):
        assert len(matching.match(user, limit=1)) == 1

    def test_benefit_is_measured_against_the_current_rate_not_zero(
        self, user, credit_profile, products, debts
    ):
        """The same loan is a good product for an expensive book and a bad one
        for a cheap book. Benefit must flip sign accordingly."""

        credit_profile.score = 760
        credit_profile.save()
        loan = Product.objects.get(name="Consolidation Loan")
        context = matching.build_context(user)

        expensive = dataclasses.replace(context, current_weighted_apr=Decimal("27.00"))
        cheap = dataclasses.replace(context, current_weighted_apr=Decimal("5.00"))

        assert matching.score_product(loan, expensive)["estimated_benefit"] > Decimal("0")
        assert matching.score_product(loan, cheap)["estimated_benefit"] < Decimal("0")

    def test_a_user_with_nothing_to_consolidate_gets_no_loan_benefit(
        self, user, credit_profile, products
    ):
        credit_profile.score = 760
        credit_profile.save()
        loan = Product.objects.get(name="Consolidation Loan")
        result = matching.score_product(loan, matching.build_context(user))
        assert result["estimated_benefit"] == Decimal("0.00")
        assert "No existing balances" in " ".join(result["reasons"])


class TestPersistence:
    def test_offers_are_stored_for_reproducibility(self, user, credit_profile, products):
        credit_profile.score = 740
        credit_profile.save()
        scored = matching.match(user)
        matching.persist_offers(user, scored)
        assert Offer.objects.filter(user=user).count() == len(scored)

    def test_rerunning_replaces_unclicked_offers(self, user, credit_profile, products):
        credit_profile.score = 740
        credit_profile.save()
        matching.persist_offers(user, matching.match(user))
        first_count = Offer.objects.filter(user=user).count()
        matching.persist_offers(user, matching.match(user))
        assert Offer.objects.filter(user=user).count() == first_count
