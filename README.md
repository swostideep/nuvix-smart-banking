# NuviX — Smart Banking & Lending Platform

A Django/DRF backend for a consumer fintech serving middle-income households:
**build credit, get out of debt, and find credit that is actually better than
what you already have** — plus the marketing analytics needed to decide where
acquisition spend should go.

Built as a ground-up rewrite of [an earlier Express/MongoDB credit-risk
prototype](https://github.com/Shail-ja/project-NuviX) — a team project whose
Jupyter notebooks explored LightGBM credit scoring and a PyTorch default-risk
classifier. Nothing of that stack survives here except the modelling ideas,
which now live behind a versioned registry in `nuvix/credit/engine/`. The
rewrite is restructured around the parts of the problem that are genuinely
hard: the payoff optimisation, the scoring model, the communications
guardrails, and the SQL.

```
270 unit tests + 75 integration tests · 92% coverage · zero lint findings · clean OpenAPI schema
```

---

## Why this exists

A household living paycheque to paycheque has three questions, and a generic
banking API answers none of them:

1. **"How much can I spend today without missing rent?"** — not the balance,
   the *trough* of the projected balance curve before the next payday.
2. **"What order should I pay these off in?"** — where the textbook answer
   (highest APR first) is provably wrong as soon as a 0% promotional balance
   is involved.
3. **"Is this loan better than what I have?"** — which is a question about the
   rate they pay *today*, not about the headline APR.

Each of those is a subsystem here, and each one is where the interesting code
lives.

---

## Quick start

```bash
git clone https://github.com/swostideep/nuvix-smart-banking.git
cd nuvix-smart-banking

make venv install      # Python 3.12 virtualenv + dependencies
make migrate
make seed              # 120 users with coherent debts, transactions, funnels
make run               # http://localhost:8000/api/docs/
```

Or the whole stack — API, Postgres, Redis, Celery worker, beat scheduler:

```bash
make up                # docker compose up + seed
```

```bash
make test              # 345 tests (4 skip without the optional ML stack)
make check             # lint + tests, exactly what CI runs
```

The seed data is generated from a fixed RNG seed, so every number in this
README is reproducible from a clean clone.

---

## The four engines

### 1. Debt payoff optimisation

`nuvix/debt/services/` — the centrepiece.

A single month-by-month simulator scores every strategy, so avalanche,
snowball and the optimiser are always compared on identical arithmetic.
Interest accrues, contractual minimums are paid, and the surplus cascades
down a priority order — each cleared balance accelerating the next.

**The interesting part:** avalanche (highest APR first) is provably optimal
*when every rate is constant*. The target market's most common debt structure
breaks that assumption — a balance transfer at 0% for nine months that reverts
to 26.99%. Greedy-on-today's-rate ignores that balance until the cliff
arrives.

So `optimal` searches. Three informed seed orderings are refined by
steepest-descent local search over adjacent transpositions, scored on the
exact simulator, and raced against the dynamic strategies. It returns the
cheapest of either family, so it is never worse than plain avalanche.

Measured on a three-debt book with one promotional balance:

| Strategy | Months | Total interest |
|---|---:|---:|
| Snowball | 16 | $767.45 |
| Avalanche | 16 | $719.42 |
| **Optimised** | **16** | **$657.45** |
| *Minimum payments only* | *80* | *$7,873.70* |

The optimiser's result matches exhaustive brute-force search over all
orderings — [asserted in the test suite](nuvix/debt/tests/test_strategies.py),
not claimed.

Also here: `budget_for_target_months` inverts the question — "what do I need
to pay monthly to be debt-free in 18 months?" — by binary search on dollars,
about 20 simulations instead of tens of thousands.

### 2. Credit scoring and simulation

`nuvix/credit/engine/` — an additive factor model at the bureaus' published
weights (payment history 35%, utilisation 30%, age 15%, mix 10%, new credit
10%), scoring 300–850.

Additive and transparent **by constraint, not by preference**: US fair-lending
rules require specific reason codes on an adverse decision, and a
gradient-boosted ensemble cannot produce them honestly.

The what-if simulator re-scores a transformed profile rather than estimating
a delta, so it can never disagree with the model it is simulating. It knows
that opening a card helps utilisation but costs an inquiry, and that closing
your oldest card is the most common self-inflicted score wound.

Risk inference sits behind a **versioned registry**: with no artifact present
it serves a transparent hand-calibrated scorecard — stated plainly as such,
not dressed up as a trained model. Point `ACTIVE_PD_MODEL` at an artifact from
`scripts/train_models.py` and fitted models take over with no code change.
The API boots and serves with no ML stack installed at all.

### 3. Lending marketplace

`nuvix/marketplace/services/matching.py` — eligibility filtered in SQL,
pricing interpolated within each product's advertised band, then ranked by
**expected value to the user**: modelled dollar benefit × approval odds.

Two decisions worth calling out:

- **Approval odds are never rounded up to certainty.** Sitting exactly on a
  product's minimum score is a coin flip, not a yes. A marketplace that
  implies certainty costs the user a hard inquiry.
- **Partner payout is recorded and deliberately excluded from ranking.**
  Ranking by revenue is what turns a recommendation engine into an ad
  network. There is [a test asserting that a 40× payout increase does not
  reorder a single offer](nuvix/marketplace/tests/test_matching.py).

### 4. Financial communications

`nuvix/comms/services/` — the hard part of financial messaging is deciding
what *not* to send.

Triggers evaluate a user's state and mostly return nothing. Survivors are
deduplicated by a content hash (so the sweep is idempotent), filtered by
per-rule cooldowns, raced on a priority heap, and finally metered by a
**token bucket** — chosen over a fixed window because a fixed window lets a
user capped at four a week receive four on Sunday night and four on Monday
morning.

Suppressed messages are written to the database *with their reason*. Knowing
what NuviX chose not to send is the only way to tell an over-eager rule from
a quiet week.

---

## Marketing analytics: hands-on SQL

`nuvix/analytics/queries.py` — raw SQL, deliberately. Cohort retention,
channel payback and multi-touch attribution are set-shaped problems wanting
window functions, `ROW_NUMBER` partitions and self-joins. Through an ORM they
get longer, slower and harder to check against the numbers a finance team will
quote back at you.

From the seeded dataset (`make seed`):

```
channel          users   spend   CAC  act%  sub%  revenue   ROAS  payback
referral            14     252    18   71%   57%     1039   4.12    2.4mo
content              6     144    24   83%   33%      339   2.35    5.8mo
paid_search         44    2728    62   59%   23%     1535   0.56   15.7mo
paid_social         25    1200    48   12%    0%        0      -        -
affiliate           11    1078    98   18%    0%        0      -        -
```

The actionable read: **referral pays back in 2.4 months and converts 57% —
fund it. Paid social and affiliate have returned nothing on $2,278 — cut
them.** That is the JD's "generating actionable insights to drive operational
decisions" as an actual query, not a claim.

Attribution is reported under three models at once, because they disagree
systematically and a budget set on one over-funds whichever end of the journey
that model favours:

```
channel          first_touch  last_touch  linear   gap
paid_search               10           7    8.33    +3
organic_search             9           5    7.00    +4
referral                   8           9    8.00    -1
paid_social                0           3    2.50    -3
```

Paid search *discovers* users; paid social *closes* them. First-touch alone
would defund the closer.

### Portability without ORM escape hatches

Production is Postgres; the test suite runs on SQLite for speed. Date
functions are the least standardised part of SQL, so
`nuvix/analytics/sql/dialect.py` emits per-backend fragments and **CI runs the
entire suite twice — once on each** — because the raw SQL is exactly the code
most able to pass on one and fail on the other.

---

## Architecture

```
config/          settings (base/dev/test/prod/ci_postgres), URLs, Celery
nuvix/
  core/          base models, Decimal money, error envelope, permissions
  accounts/      email-first user, financial profile, JWT
  banking/       linked accounts, transactions, recurrence detection, cashflow
  credit/        factor scoring, what-if simulator, versioned risk registry
  debt/          amortisation simulator, strategies, payoff optimiser
  marketplace/   partner catalogue, eligibility, approval odds, ranking
  comms/         templates, triggers, token bucket, dispatcher
  analytics/     event log, raw-SQL reports, SQL dialect shim
```

**The engines never import a model.** Every algorithm takes frozen dataclasses
and returns frozen dataclasses, touching neither the ORM nor the clock. A
single `to_input()` method per model is the whole boundary. That is why the
payoff engine's 38 tests run in a tenth of a second with no database, and why a
schedule shown to a customer in January can be reproduced exactly in June.

Further reading:

- [`docs/architecture.md`](docs/architecture.md) — layering, request lifecycle, scaling
- [`docs/algorithms.md`](docs/algorithms.md) — every algorithm with complexity and rationale
- [`docs/api.md`](docs/api.md) — endpoint reference
- [`docs/decisions/`](docs/decisions/) — ADRs for the choices that were close calls

---

## Design decisions worth defending

**Decimal everywhere, floats banned in the engines.** A payoff schedule
iterates hundreds of times; binary floating-point drift compounds into visibly
wrong dollar figures on a customer's statement.

**Monthly rate is `APR / 12`, not the geometric equivalent.** That is how US
issuers compute a periodic rate. Matching the issuer's arithmetic matters more
than theoretical purity — a projection that disagrees with a real statement
destroys trust.

**Soft deletes on financial records.** Deleting a debt must not invalidate the
payoff plans that referenced it.

**Suppressed messages are rows.** So are superseded payoff plans. What a
customer was told, and when, is part of the audit trail.

**UUID primary keys.** Record ids reach mobile clients; sequential integers
leak how many users exist and invite enumeration.

---

## Bugs this project found in itself

Kept here because the interesting part of a codebase is what it caught, and
each of these has a regression test:

| Bug | Why it mattered |
|---|---|
| `permission_classes = [IsOwner]` **replaced** the default `IsAuthenticated` instead of adding to it | Every collection route was open to anonymous callers. It failed *quietly* — the queryset filtered on `AnonymousUser` and returned a confusing 400 rather than a 401, reading like a validation bug. |
| Error envelope reported `default_code` instead of the per-raise code | Every `DomainError` subclass collapsed to `"domain_error"`, making the specific codes clients switch on useless. |
| `strftime('%Y', …)` in analytics SQL | Django's SQLite backend renders debug queries with `sql % params`. Passed with `DEBUG=False`, crashed the moment a developer opened the report locally. |
| Dialect fragments duplicated any bound parameter passed into them | `months_between` references its argument twice, so a `%s` inside was emitted twice and bound once. |
| `safe_divide` did not parenthesise its arguments | `SUM(a) + SUM(b)` over `spend` bound the division to `SUM(b)` alone. ROAS reported **1535.20** where the true ratio was **0.56** — the query ran, returned a plausible number, and was simply wrong. Caught by checking a README table against a live run. |
| Token bucket divided by zero at capacity 0 | Capacity 0 is the legitimate way to disable a channel. |
| Funnel reported negative drop-offs and >100% conversion | An un-instrumented step read as zero, so the next step "gained" users. |
| The first-ever score was reported as a **+347 point jump** | A never-scored profile carries the model floor (300) as a placeholder, not a previous score. The communications engine cheerfully told brand-new users their score had "moved up 347 points". Found by running the app, not by a test. |
| A profile with no credit history scored 624 | It inherited full marks for a payment record it did not have — and every downstream decision built on that number was wrong. |
| `interest_saved` went **negative** for the worst possible debt book | A baseline that never amortises stops early, so its interest is a partial sum. Subtracting it reported a negative saving for the users who need the plan most. |

---

## Tech

Python 3.12 · Django 5.1 · DRF · PostgreSQL · Redis · Celery · Docker ·
GitHub Actions · pytest · ruff · drf-spectacular

## Licence

MIT — see [LICENSE](LICENSE).
