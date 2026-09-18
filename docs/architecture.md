# Architecture

## The shape of the system

NuviX is a Django/DRF monolith with a Celery worker beside it. That is a
deliberate choice, not a default — see
[ADR-0001](decisions/0001-modular-monolith.md). The important structure is not
service boundaries but a **layering rule that every app obeys**.

```
HTTP  →  View  →  Serializer  →  Service  →  Engine
                                    ↓          ↑
                                  Models   (pure functions,
                                            frozen dataclasses)
```

| Layer | Responsibility | May import |
|---|---|---|
| **View** | HTTP concerns: auth, status codes, throttle scope | serializers, services |
| **Serializer** | Shape and validate the wire format | models, engine value objects |
| **Service** | Orchestration: read the ORM, call the engine, write back | models, engines |
| **Engine** | The algorithm. Pure. | `decimal`, `datetime`, stdlib |
| **Model** | Persistence, invariants, one `to_input()` adapter | Django ORM |

### The rule that matters

**An engine never imports a model.** `nuvix/debt/services/amortization.py`
imports `Decimal` and `datetime` and nothing else from this project except
value objects. The same holds for `credit/engine/factors.py`,
`banking/services/recurrence.py` and `comms/services/ratelimit.py`.

Three things follow:

1. **Speed.** The payoff engine's 38 tests run in 0.07 seconds with no
   database. That makes it cheap to test the algorithm exhaustively — the
   optimiser is verified against brute-force search over every permutation.
2. **Reproducibility.** An engine takes frozen dataclasses and returns frozen
   dataclasses, touching neither the ORM nor the clock. A schedule shown to a
   customer in January reproduces exactly in June from the same inputs, which
   matters the first time someone disputes a projection.
3. **Reviewability.** A reviewer can read the payoff algorithm without knowing
   the schema.

The cost is one adapter method per model (`Debt.to_input()`,
`CreditProfile.to_inputs()`). That is the entire bridge.

## Request lifecycle

```
Client
  │  Authorization: Bearer <JWT>          (X-Request-ID optional)
  ▼
RequestIDMiddleware      attach/propagate a correlation id
  ▼
JWTAuthentication        simplejwt, 30-minute access token
  ▼
Permission               IsOwner / IsStaffOrReadOnly / IsAdminUser
  ▼
ScopedRateThrottle       "simulation" 60/min, "auth" 20/min, "analytics" 120/min
  ▼
Serializer.is_valid()    reject malformed input before any engine runs
  ▼
Service                  queryset scoped to request.user
  ▼
Engine                   pure computation
  ▼
Response                 JSON, X-Request-ID echoed
```

Failures at any stage leave through one exception handler and arrive in one
shape:

```json
{"error": {"code": "insufficient_budget",
           "message": "A budget of at least 455.00 is required...",
           "details": {"minimum_required": "455.00", "provided": "300.00"},
           "request_id": "9feaf9e6..."}}
```

Mobile clients switch on `code`. Anything unhandled becomes
`{"code": "internal_error"}` with the traceback logged against the request id
and nothing internal on the wire.

## Security model

The invariant that must never break: **a user can only ever read or write
their own financial records.**

Two independent layers enforce it:

1. Every user-scoped view filters its queryset by `request.user`.
2. `IsOwner` re-checks ownership on detail routes.

Both exist because either alone has failed in practice. This project shipped a
bug where `permission_classes = [IsOwner]` *replaced* the project default of
`IsAuthenticated` rather than adding to it, leaving collection routes open to
anonymous callers — and failing *quietly*, with a confusing 400 from the
queryset rather than a 401. `IsOwner` now implements `has_permission` too, and
[a parametrised test asserts a 401 on every such route](../nuvix/tests/test_api_auth.py).

Other deliberate choices:

- **UUID primary keys.** Record ids reach mobile clients; sequential integers
  leak user counts and invite enumeration.
- **Analytics reports are staff-only.** They aggregate across all users, a
  categorically different exposure from a single-user leak.
- **`DJANGO_SECRET_KEY` has no default in production settings**, so a
  misconfigured deploy fails at boot rather than running on a shared key.
- **Touchpoint ingestion requires authentication.** An open endpoint writing
  rows keyed by an attacker-supplied `anonymous_id` is an attribution-poisoning
  primitive.

## Data model

```
User ─1:1─ Profile                  income, employment, dependents
  │
  ├─1:1─ CreditProfile ──*── ScoreSnapshot      score history (on change only)
  │            └──*── RiskAssessment           PD + decision + reason codes
  │
  ├──*── LinkedAccount ──*── Transaction
  │            └──*── RecurringSeries          materialised detection output
  │
  ├──*── Debt ──*── PaymentRecord
  │       └── PayoffPlan                        one active, history retained
  │
  ├──*── Offer ──── Product ──── Partner
  ├──*── Message ── CommunicationRule ── MessageTemplate
  └──*── TouchPoint, FunnelEvent, Subscription  (analytics event log)
```

Notes on choices that were not obvious:

- **`Debt` is separate from `LinkedAccount`.** Plenty of users carry a medical
  bill or a loan from a lender NuviX cannot aggregate. Refusing to plan around
  unlinkable debt would make every plan wrong.
- **Balances are signed from the user's point of view** — positive is money
  they have, negative is money they owe — across both deposit and credit
  accounts, so the cashflow engine never branches on account type to add two
  balances.
- **Analytics is an append-only event log**, not a `channel` column on `User`.
  First-touch versus last-touch attribution cannot be recomputed from a single
  denormalised column.
- **Soft deletes on financial records.** Archiving a debt must not invalidate
  the payoff plans that referenced it.
- **The payoff schedule is not stored.** It is derived, large, and stale the
  moment a balance moves. The summary plus the inputs are stored, which is
  enough to reproduce it and to show what a customer was told.

## Background work

| Task | Schedule | What it does |
|---|---|---|
| `refresh_stale_credit_profiles` | 03:00 | Rescore profiles untouched for 7+ days |
| `recompute_active_plans` | 04:00 | Refresh payoff plans against current balances |
| `run_daily_sweep` | 14:00 | Fan out one communications task per active user |
| `dispatch_due_messages` | every 15 min | Deliver messages whose send time has arrived |

Two patterns throughout:

**Fan out, don't batch.** `run_daily_sweep` enqueues one task per user rather
than sweeping everyone in a single task. A long task holding a transaction
across thousands of users blocks, and one user's failure rolls back everyone
else's messages.

**Cap every batch.** Each task takes a `limit`. A nightly job with unbounded
runtime is an outage waiting for the user table to grow; anything not reached
tonight is simply the most stale batch tomorrow.

## Scaling

What would need to change, in the order it would bite:

| Pressure | Response |
|---|---|
| Transaction table growth | Already cursor-paginated and indexed on `(user, -posted_on)`. Next: partition by month. |
| Analytics queries slowing | The event log is the read-heavy part; move reports to a read replica, then to nightly materialised views. |
| Payoff simulation CPU | Bounded at `MAX_EVALUATIONS` simulations. Beyond that, memoise on a hash of the debt book — plans change far less often than they are viewed. |
| Rate-limiter contention | `consume()` is a read-modify-write and knowingly racy; the cost of the race is one extra message. A hard cap needs a Redis Lua script doing check-and-decrement atomically — not a lock on the send path. |
| Marketplace catalogue growth | Eligibility is already a SQL `WHERE`. Ranking is `O(n)` over eligible products; cache per `(score band, DTI band)`. |

## Environments

`config/settings/` splits into `base` + four overrides, each changing only
what genuinely differs:

- **`dev`** — DEBUG on, LocMem cache, eager Celery. No Redis needed on a laptop.
- **`test`** — in-memory SQLite, MD5 hashing, logging off. Hermetic and fast.
- **`ci_postgres`** — the test suite against Postgres. CI runs both, because
  the raw analytics SQL is exactly the code most able to pass on one backend
  and fail on the other.
- **`prod`** — secrets required, HSTS, secure cookies, SSL redirect.
