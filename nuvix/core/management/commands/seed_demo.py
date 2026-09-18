"""Generate a coherent demo dataset.

Every engine in NuviX reads from several tables at once, so seeding has to
produce a *consistent* world rather than random rows: a user's debts must
match their credit profile's utilisation, their transactions must contain the
paycheque their cashflow projection expects, and their marketing touches must
precede the signup they are credited with. Random data satisfies none of that,
and a demo built on it makes every report look broken.

Usage::

    python manage.py seed_demo --users 60
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from nuvix.accounts.models import EmploymentStatus, User
from nuvix.analytics.models import (
    Channel,
    FunnelEvent,
    FunnelStep,
    MarketingSpend,
    MarketplaceRevenue,
    Subscription,
    TouchPoint,
)
from nuvix.banking.models import (
    AccountType,
    Institution,
    LinkedAccount,
    Transaction,
    TransactionCategory,
)
from nuvix.comms.models import Channel as CommsChannel
from nuvix.comms.models import CommunicationRule, MessageTemplate, TriggerType
from nuvix.credit.models import CreditProfile
from nuvix.credit.services import scoring
from nuvix.debt.models import Debt, DebtKind
from nuvix.marketplace.models import Partner, Product, ProductType

DEMO_PASSWORD = "nuvix-demo-password"

INSTITUTIONS = [
    ("First Federal Bank", "first-federal"),
    ("Cascade Credit Union", "cascade-cu"),
    ("Meridian Financial", "meridian"),
]

#: ``(channel, target CAC, share of first touches, downstream quality)``.
#:
#: The target CAC and the quality score are deliberately *anti-correlated* for
#: some channels and aligned for others: affiliate is expensive and converts
#: poorly, referral is cheap and converts well, paid search is expensive but
#: high volume. That is what makes the channel report a decision rather than a
#: table -- a demo where the cheapest channel is also the best teaches nothing.
CHANNEL_MIX = [
    (Channel.PAID_SEARCH, Decimal("62.00"), 0.30, 0.45),
    (Channel.PAID_SOCIAL, Decimal("48.00"), 0.26, 0.28),
    (Channel.ORGANIC_SEARCH, Decimal("0.00"), 0.16, 0.55),
    (Channel.REFERRAL, Decimal("18.00"), 0.12, 0.68),
    (Channel.CONTENT, Decimal("24.00"), 0.10, 0.50),
    (Channel.AFFILIATE, Decimal("98.00"), 0.06, 0.35),
]

FIRST_NAMES = [
    "Ada",
    "Marcus",
    "Priya",
    "Elena",
    "Tomas",
    "Nia",
    "Rafael",
    "Grace",
    "Omar",
    "Leah",
    "Kenji",
    "Maya",
    "Dev",
    "Iris",
    "Samir",
    "Noor",
]
LAST_NAMES = [
    "Chen",
    "Okafor",
    "Nowak",
    "Silva",
    "Haddad",
    "Larsen",
    "Mbeki",
    "Rossi",
    "Kaur",
    "Fischer",
    "Almeida",
    "Novak",
    "Ibrahim",
    "Tanaka",
]


class Command(BaseCommand):
    help = "Seed a coherent demo dataset across every NuviX app."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--users", type=int, default=40)
        parser.add_argument(
            "--seed",
            type=int,
            default=20260101,
            help="RNG seed; the default makes the dataset reproducible.",
        )
        parser.add_argument(
            "--flush", action="store_true", help="Delete existing demo users first."
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        # A fixed seed by default: a demo that shows different numbers every
        # time it is run is impossible to write documentation against.
        rng = random.Random(options["seed"])
        count = options["users"]

        if options["flush"]:
            deleted, _ = User.objects.filter(email__endswith="@demo.nuvix.test").delete()
            self.stdout.write(f"Removed {deleted} demo rows")

        institutions = self._institutions()
        self._catalogue()
        self._comms_rules()

        today = timezone.localdate()
        acquisitions: dict[str, list[dt.date]] = {}
        created = 0
        for index in range(count):
            user = self._user(rng, index, today)
            channel, acquired_on = self._acquisition(rng, user, today)
            acquisitions.setdefault(channel, []).append(acquired_on)
            self._banking(rng, user, institutions, today)
            self._debts(rng, user)
            self._credit(rng, user)
            self._monetisation(rng, user, today)
            created += 1

        # Spend is derived from the users actually acquired, not invented
        # independently. Seeding them separately is how a demo ends up
        # reporting a five-figure CAC and every report looks broken.
        self._marketing_spend(rng, acquisitions)

        self.stdout.write(self.style.SUCCESS(f"Seeded {created} demo users."))
        self.stdout.write(f"Sign in with any listed email and password {DEMO_PASSWORD!r}.")
        self.stdout.write("Reports: /api/v1/analytics/reports/channels (staff only).")

    # -- reference data ----------------------------------------------------

    def _institutions(self) -> list[Institution]:
        return [
            Institution.objects.get_or_create(slug=slug, defaults={"name": name})[0]
            for name, slug in INSTITUTIONS
        ]

    def _catalogue(self) -> None:
        harbor, _ = Partner.objects.get_or_create(
            slug="harbor-lending",
            defaults={"name": "Harbor Lending", "payout_per_funded": Decimal("145.00")},
        )
        northwind, _ = Partner.objects.get_or_create(
            slug="northwind-card",
            defaults={"name": "Northwind Card", "payout_per_funded": Decimal("85.00")},
        )

        specs = [
            (
                harbor,
                "Consolidation Loan",
                ProductType.DEBT_CONSOLIDATION,
                "8.99",
                "21.99",
                640,
                "0.450",
                "35000",
                2000,
                30000,
                "0.0300",
                "0.700",
            ),
            (
                harbor,
                "Personal Loan",
                ProductType.PERSONAL_LOAN,
                "10.99",
                "24.99",
                600,
                "0.500",
                "28000",
                1000,
                20000,
                "0.0500",
                "0.650",
            ),
            (
                northwind,
                "Everyday Rewards Card",
                ProductType.CREDIT_CARD,
                "19.99",
                "28.99",
                700,
                "0.400",
                "45000",
                0,
                9000,
                "0",
                "0.480",
            ),
            (
                northwind,
                "Starter Secured Card",
                ProductType.SECURED_CARD,
                "24.99",
                "27.99",
                300,
                "1.000",
                "0",
                200,
                1000,
                "0",
                "0.950",
            ),
            (
                harbor,
                "Credit Builder Loan",
                ProductType.CREDIT_BUILDER,
                "5.99",
                "14.99",
                300,
                "0.900",
                "12000",
                300,
                3000,
                "0",
                "0.900",
            ),
        ]
        for (
            partner,
            name,
            kind,
            apr_min,
            apr_max,
            score,
            dti,
            income,
            amount_min,
            amount_max,
            origination,
            approval,
        ) in specs:
            Product.objects.get_or_create(
                partner=partner,
                name=name,
                defaults={
                    "product_type": kind,
                    "apr_min": Decimal(apr_min),
                    "apr_max": Decimal(apr_max),
                    "min_credit_score": score,
                    "max_dti_ratio": Decimal(dti),
                    "min_annual_income": Decimal(income),
                    "amount_min": Decimal(amount_min),
                    "amount_max": Decimal(amount_max),
                    "origination_fee_rate": Decimal(origination),
                    "historical_approval_rate": Decimal(approval),
                    "term_months_min": 24,
                    "term_months_max": 60,
                    "rewards_rate": Decimal("0.0175")
                    if kind == ProductType.CREDIT_CARD
                    else Decimal("0"),
                    "annual_fee": Decimal("0"),
                    "intro_apr": Decimal("0") if kind == ProductType.CREDIT_CARD else None,
                    "intro_apr_months": 12 if kind == ProductType.CREDIT_CARD else 0,
                },
            )

    def _comms_rules(self) -> None:
        specs = [
            (
                "utilisation-high",
                TriggerType.UTILIZATION_HIGH,
                CommsChannel.PUSH,
                90,
                14,
                1,
                "You're {utilization_pct}% used",
                "Hi {first_name} -- paying ${paydown_amount} before your statement could "
                "add about {points_gain} points to your score.",
                {"threshold": "0.30"},
            ),
            (
                "shortfall-warning",
                TriggerType.PROJECTED_SHORTFALL,
                CommsChannel.SMS,
                100,
                3,
                2,
                "Heads up before {shortfall_date}",
                "{first_name}, you're on track to be about ${shortfall_amount} short on "
                "{shortfall_date}. Safe to spend today: ${safe_to_spend}.",
                {"horizon_days": 21},
            ),
            (
                "payment-due",
                TriggerType.PAYMENT_DUE_SOON,
                CommsChannel.PUSH,
                85,
                25,
                2,
                "{debt_name} is due in {due_in_days} days",
                "Minimum due is ${minimum_payment} at {apr}% APR.",
                {"days_ahead": 4},
            ),
            (
                "score-moved",
                TriggerType.SCORE_CHANGED,
                CommsChannel.PUSH,
                60,
                7,
                2,
                "Your score moved {direction} {delta} points",
                "You're now at {score} -- {band}.",
                {"min_delta": 8},
            ),
            (
                "payoff-milestone",
                TriggerType.PAYOFF_MILESTONE,
                CommsChannel.PUSH,
                55,
                30,
                1,
                "{milestone_pct}% paid off",
                "You've cleared ${cleared_amount}. Debt-free on {debt_free_date}.",
                {},
            ),
            (
                "build-a-plan",
                TriggerType.NO_PLAN,
                CommsChannel.EMAIL,
                30,
                21,
                1,
                "You could save ${interest_saved}",
                "{first_name}, a plan at ${monthly_budget}/month would save about "
                "${interest_saved} and {months_saved} months.",
                {},
            ),
            (
                "better-offer",
                TriggerType.BETTER_OFFER,
                CommsChannel.EMAIL,
                40,
                30,
                1,
                "A better rate than you're paying now",
                "{partner_name}'s {product_name} at {estimated_apr}% could be worth about "
                "${estimated_benefit}. Approval odds {approval_odds_pct}%.",
                {"min_benefit": "250", "min_odds": "0.5"},
            ),
        ]
        for key, trigger, channel, priority, cooldown, cap, subject, body, params in specs:
            template, _ = MessageTemplate.objects.get_or_create(
                key=key,
                defaults={"channel": channel, "subject": subject, "body": body},
            )
            CommunicationRule.objects.get_or_create(
                key=key,
                defaults={
                    "trigger": trigger,
                    "template": template,
                    "priority": priority,
                    "cooldown_days": cooldown,
                    "max_per_week": cap,
                    "parameters": params,
                },
            )

    def _marketing_spend(
        self, rng: random.Random, acquisitions: dict[str, list[dt.date]]
    ) -> None:
        """Spread each channel's budget over the days it actually acquired on.

        Total spend per channel is ``users x target CAC``, jittered per day so
        the series looks like a real account rather than a flat line, and
        placed in the months where that channel's signups happened so the
        report's date filtering has something honest to do.
        """

        targets = {channel: cac for channel, cac, _, _ in CHANNEL_MIX}

        for channel, dates in acquisitions.items():
            target_cac = targets.get(channel, Decimal("0"))
            if target_cac <= 0 or not dates:
                continue

            budget = target_cac * Decimal(len(dates))
            # Spend lands in the week before each signup it paid for.
            weights = [rng.uniform(0.6, 1.4) for _ in dates]
            total_weight = Decimal(str(sum(weights)))

            for acquired_on, weight in zip(dates, weights, strict=True):
                day = acquired_on - dt.timedelta(days=rng.randint(0, 6))
                amount = (budget * Decimal(str(weight)) / total_weight).quantize(
                    Decimal("0.01")
                )
                row, was_created = MarketingSpend.objects.get_or_create(
                    channel=channel,
                    campaign="always-on",
                    spend_on=day,
                    defaults={
                        "amount": amount,
                        "clicks": int(amount / Decimal("1.8")),
                        "impressions": int(amount * 40),
                    },
                )
                if not was_created:
                    row.amount += amount
                    row.clicks += int(amount / Decimal("1.8"))
                    row.impressions += int(amount * 40)
                    row.save(update_fields=["amount", "clicks", "impressions"])

    # -- per-user data -----------------------------------------------------

    def _user(self, rng: random.Random, index: int, today: dt.date) -> User:
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        joined = today - dt.timedelta(days=rng.randint(5, 540))

        user = User.objects.create_user(
            email=f"{first.lower()}.{last.lower()}{index}@demo.nuvix.test",
            password=DEMO_PASSWORD,
            first_name=first,
            last_name=last,
        )
        User.objects.filter(pk=user.pk).update(
            date_joined=timezone.make_aware(dt.datetime.combine(joined, dt.time(10, 0)))
        )
        user.refresh_from_db()

        annual = Decimal(rng.randrange(34_000, 96_000, 1000))
        profile = user.profile
        profile.annual_income = annual
        profile.monthly_net_income = (annual / Decimal(12) * Decimal("0.78")).quantize(
            Decimal("0.01")
        )
        profile.monthly_fixed_expenses = (
            profile.monthly_net_income * Decimal(str(rng.uniform(0.5, 0.72)))
        ).quantize(Decimal("0.01"))
        profile.months_employed = rng.randint(3, 140)
        profile.dependents = rng.choice([0, 0, 1, 2, 3])
        profile.has_mortgage = rng.random() < 0.28
        profile.employment_status = rng.choice(
            [EmploymentStatus.FULL_TIME] * 7
            + [
                EmploymentStatus.PART_TIME,
                EmploymentStatus.SELF_EMPLOYED,
                EmploymentStatus.CONTRACT,
            ]
        )
        profile.date_of_birth = today - dt.timedelta(days=365 * rng.randint(23, 58))
        profile.state = rng.choice(["WA", "OR", "CA", "TX", "FL", "NY", "IL", "GA"])
        profile.onboarding_completed_at = timezone.now()
        profile.save()
        return user

    def _acquisition(
        self, rng: random.Random, user: User, today: dt.date
    ) -> tuple[str, dt.date]:
        joined = user.date_joined.date()
        channels, weights = zip(*[(c, w) for c, _, w, _ in CHANNEL_MIX], strict=False)
        first_channel = rng.choices(channels, weights=weights)[0]
        quality = next(q for c, _, _, q in CHANNEL_MIX if c == first_channel)

        touches = [(first_channel, joined - dt.timedelta(days=rng.randint(3, 30)))]
        for _ in range(rng.randint(0, 2)):
            touches.append(
                (
                    rng.choices(channels, weights=weights)[0],
                    joined - dt.timedelta(days=rng.randint(0, 2)),
                )
            )

        for channel, when in touches:
            TouchPoint.objects.create(
                user=user,
                channel=channel,
                campaign="always-on",
                utm_source=channel,
                utm_medium="cpc" if "paid" in channel else "organic",
                landing_path="/debt-payoff",
                occurred_at=timezone.make_aware(dt.datetime.combine(when, dt.time(9, 30))),
            )

        # A funnel that narrows, with channel quality driving how far each user
        # gets -- which is what makes the channel report say something.
        # Stage probabilities are tuned so that roughly one signup in six ends
        # up subscribed. Compounding four independent gates makes it very easy
        # to end up with two paying users in a hundred, at which point the
        # revenue reports have no signal and the demo cannot show its work.
        steps = [FunnelStep.VISIT, FunnelStep.SIGNUP]
        if rng.random() < quality + 0.25:
            steps.append(FunnelStep.PROFILE_COMPLETE)
            if rng.random() < quality + 0.15:
                steps.append(FunnelStep.ACCOUNT_LINKED)
                if rng.random() < quality + 0.25:
                    steps.append(FunnelStep.PLAN_CREATED)
                    if rng.random() < quality + 0.15:
                        steps.append(FunnelStep.SUBSCRIBED)

        for offset, step in enumerate(steps):
            FunnelEvent.objects.create(
                user=user,
                step=step,
                occurred_at=timezone.make_aware(
                    dt.datetime.combine(joined + dt.timedelta(days=offset), dt.time(11, 0))
                ),
            )
        user._demo_steps = set(steps)
        return first_channel, joined

    def _banking(
        self, rng: random.Random, user: User, institutions: list[Institution], today: dt.date
    ) -> None:
        institution = rng.choice(institutions)
        checking = LinkedAccount.objects.create(
            user=user,
            institution=institution,
            name="Everyday Checking",
            account_type=AccountType.CHECKING,
            mask=f"{rng.randint(1000, 9999)}",
            current_balance=Decimal(str(round(rng.uniform(120, 3200), 2))),
            available_balance=Decimal(str(round(rng.uniform(120, 3200), 2))),
            is_primary_checking=True,
            last_synced_at=timezone.now(),
        )

        net = user.profile.monthly_net_income
        paycheque = (net / Decimal(2)).quantize(Decimal("0.01"))
        rent = (net * Decimal("0.32")).quantize(Decimal("0.01"))

        rows: list[Transaction] = []
        for index in range(18):
            rows.append(
                Transaction(
                    account=checking,
                    user=user,
                    posted_on=today - dt.timedelta(days=14 * index),
                    amount=paycheque,
                    merchant="ACME PAYROLL DIRECT DEP",
                    category=TransactionCategory.INCOME,
                )
            )
        for index in range(9):
            rows.append(
                Transaction(
                    account=checking,
                    user=user,
                    posted_on=today - dt.timedelta(days=30 * index + 2),
                    amount=-rent,
                    merchant="GREENLEAF PROPERTY MGMT",
                    category=TransactionCategory.HOUSING,
                )
            )
            rows.append(
                Transaction(
                    account=checking,
                    user=user,
                    posted_on=today - dt.timedelta(days=30 * index + 8),
                    amount=Decimal(str(-round(rng.uniform(88, 132), 2))),
                    merchant="CITY POWER AND LIGHT",
                    category=TransactionCategory.UTILITIES,
                )
            )
            rows.append(
                Transaction(
                    account=checking,
                    user=user,
                    posted_on=today - dt.timedelta(days=30 * index + 15),
                    amount=Decimal("-15.99"),
                    merchant="STREAMFLIX SUBSCRIPTION",
                    category=TransactionCategory.SUBSCRIPTION,
                )
            )
        for row in rows:
            row.merchant_key = ""
            row.save()

    def _debts(self, rng: random.Random, user: User) -> None:
        """Give the user a plausible debt book.

        The mix is weighted so that most users carry a card, a third carry a
        promotional balance-transfer card, and some carry instalment debt.
        The promo cards matter: they are the case where the payoff optimiser
        beats textbook avalanche, and a demo dataset without them never
        exercises the most interesting path in the engine.
        """

        specs: list[dict] = []

        if rng.random() < 0.85:
            balance = Decimal(str(round(rng.uniform(600, 7800), 2)))
            specs.append(
                {
                    "name": "Everyday Card",
                    "kind": DebtKind.CREDIT_CARD,
                    "balance": balance,
                    "apr": Decimal(str(round(rng.uniform(18.99, 29.99), 2))),
                    "minimum_payment": max(
                        (balance * Decimal("0.02")).quantize(Decimal("0.01")),
                        Decimal("25.00"),
                    ),
                    "minimum_payment_rate": Decimal("0.0200"),
                    "credit_limit": (balance * Decimal(str(rng.uniform(1.3, 3.2)))).quantize(
                        Decimal("0.01")
                    ),
                    "due_day": rng.randint(1, 28),
                }
            )

        if rng.random() < 0.35:
            balance = Decimal(str(round(rng.uniform(1500, 9000), 2)))
            specs.append(
                {
                    "name": "Balance Transfer Card",
                    "kind": DebtKind.CREDIT_CARD,
                    "balance": balance,
                    "apr": Decimal("26.99"),
                    "minimum_payment": Decimal("95.00"),
                    "promo_apr": Decimal("0.00"),
                    "promo_months_remaining": rng.randint(2, 12),
                    "credit_limit": (balance * Decimal("1.4")).quantize(Decimal("0.01")),
                    "due_day": rng.randint(1, 28),
                }
            )

        if rng.random() < 0.40:
            specs.append(
                {
                    "name": "Auto Loan",
                    "kind": DebtKind.AUTO_LOAN,
                    "balance": Decimal(str(round(rng.uniform(3000, 19000), 2))),
                    "apr": Decimal(str(round(rng.uniform(5.49, 11.99), 2))),
                    "minimum_payment": Decimal(str(round(rng.uniform(210, 480), 2))),
                    "due_day": rng.randint(1, 28),
                }
            )

        if rng.random() < 0.30:
            specs.append(
                {
                    "name": "Student Loan",
                    "kind": DebtKind.STUDENT_LOAN,
                    "balance": Decimal(str(round(rng.uniform(4000, 34000), 2))),
                    "apr": Decimal(str(round(rng.uniform(4.5, 7.5), 2))),
                    "minimum_payment": Decimal(str(round(rng.uniform(120, 340), 2))),
                    "due_day": rng.randint(1, 28),
                }
            )

        for spec in specs:
            Debt.objects.create(user=user, **spec)

    def _credit(self, rng: random.Random, user: User) -> None:
        cards = Debt.objects.alive().filter(user=user, credit_limit__isnull=False)
        total_balance = sum((d.balance for d in cards), Decimal("0"))
        total_limit = sum((d.credit_limit for d in cards), Decimal("0"))
        worst = max(
            ((d.balance / d.credit_limit) for d in cards if d.credit_limit),
            default=Decimal("0"),
        )

        profile, _ = CreditProfile.objects.get_or_create(user=user)
        profile.total_balance = total_balance
        profile.total_credit_limit = total_limit
        profile.max_single_card_utilization = Decimal(worst).quantize(Decimal("0.0001"))
        profile.on_time_payment_rate = Decimal(str(round(rng.uniform(0.86, 1.0), 3)))
        profile.late_payments_30d = rng.choice([0, 0, 0, 1, 2])
        profile.late_payments_90d = rng.choice([0, 0, 0, 0, 1])
        profile.derogatory_marks = rng.choice([0] * 9 + [1])
        profile.oldest_account_months = rng.randint(8, 190)
        profile.average_account_age_months = rng.randint(6, 90)
        profile.credit_mix_types = min(
            Debt.objects.alive().filter(user=user).values("kind").distinct().count() or 1, 4
        )
        profile.open_accounts = max(Debt.objects.alive().filter(user=user).count(), 1)
        profile.hard_inquiries_12m = rng.choice([0, 0, 1, 1, 2, 3])
        profile.accounts_opened_24m = rng.choice([0, 0, 1, 2])
        profile.save()
        scoring.rescore(user)

    def _monetisation(self, rng: random.Random, user: User, today: dt.date) -> None:
        steps = getattr(user, "_demo_steps", set())
        if FunnelStep.SUBSCRIBED not in steps:
            return

        started = user.date_joined.date() + dt.timedelta(days=rng.randint(1, 20))
        if started > today:
            started = today

        plan, price = rng.choice(
            [
                (Subscription.Plan.STARTER, Decimal("7.99")),
                (Subscription.Plan.PLUS, Decimal("14.99")),
                (Subscription.Plan.PREMIUM, Decimal("24.99")),
            ]
        )
        cancelled = None
        if rng.random() < 0.28:
            cancelled = started + dt.timedelta(days=rng.randint(30, 300))
            if cancelled > today:
                cancelled = None

        Subscription.objects.create(
            user=user,
            plan=plan,
            monthly_price=price,
            started_on=started,
            cancelled_on=cancelled,
        )

        if rng.random() < 0.18:
            MarketplaceRevenue.objects.create(
                user=user,
                amount=Decimal(str(round(rng.uniform(60, 180), 2))),
                earned_on=started + dt.timedelta(days=rng.randint(5, 60)),
            )
