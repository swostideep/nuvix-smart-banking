# API reference

Base path `/api/v1/`. Interactive docs at `/api/docs/` (Swagger) and
`/api/redoc/`; the machine-readable schema is at `/api/schema/` or via
`make schema`.

Authentication is a JWT bearer token: `Authorization: Bearer <access>`.

## Conventions

- **Errors** always arrive in one envelope. Clients switch on `code`:
  ```json
  {"error": {"code": "insufficient_budget", "message": "...",
             "details": {"minimum_required": "455.00"},
             "request_id": "9feaf9e6..."}}
  ```
- **Money** is a decimal string (`"4200.00"`), never a float. **Rates** are
  percentages (`"24.99"` means 24.99% APR).
- **Lists** are cursor-paginated (`{"next", "previous", "results"}`).
- **Date windows** are half-open: `start` inclusive, `end` exclusive.
- **Throttles**: `auth` 20/min, `simulation` 60/min, `analytics` 120/min.

---

## Accounts

| Method | Path | Notes |
|---|---|---|
| `POST` | `/accounts/register` | Returns the user **and** a token pair |
| `POST` | `/accounts/login` | Returns the user alongside the tokens, saving a round trip |
| `POST` | `/accounts/token/refresh` | Rotating refresh tokens |
| `GET PATCH` | `/accounts/me` | |
| `GET PATCH` | `/accounts/me/profile` | Completing income fields sets `onboarding_completed_at` |
| `POST` | `/accounts/me/password` | |

## Banking

| Method | Path | Notes |
|---|---|---|
| `GET` | `/banking/institutions` | |
| `GET POST PATCH DELETE` | `/banking/accounts` | `DELETE` archives, never destroys |
| `POST` | `/banking/accounts/{id}/sync` | Stands in for an aggregator webhook |
| `GET POST` | `/banking/transactions` | |
| `POST` | `/banking/transactions/bulk` | Batch ingest — a feed refresh is hundreds of rows |
| `GET` | `/banking/recurring` | Detected series |
| `POST` | `/banking/recurring/detect` | Re-run detection |
| `GET` | `/banking/cashflow?horizon_days=45` | Projection, trough and safe-to-spend |

## Credit

| Method | Path | Notes |
|---|---|---|
| `GET PATCH` | `/credit/profile` | |
| `GET` | `/credit/score` | Score, five-factor breakdown, biggest opportunity |
| `GET` | `/credit/score/history?days=365` | Snapshots, written on change only |
| `POST` | `/credit/score/refresh` | Re-derive utilisation from linked accounts |
| `POST` | `/credit/score/simulate` | What-if for one action |
| `GET` | `/credit/score/recommendations` | Realistic actions ranked by points earned |
| `POST` | `/credit/risk/assess` | PD, grade, decision, reason codes |
| `GET` | `/credit/risk/assessments` | |

Simulator actions and their required parameters:

| Action | Requires |
|---|---|
| `pay_down_balance` | `amount` |
| `increase_limit` | `amount` |
| `open_new_card` | `credit_limit` |
| `close_card` | `credit_limit`, optional `is_oldest` |
| `on_time_months` | `months` |

```bash
curl -X POST localhost:8000/api/v1/credit/score/simulate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action": "pay_down_balance", "amount": "2500.00"}'
```
```json
{"score_before": 673, "score_after": 718, "score_change": 45,
 "band_before": "good", "band_after": "good",
 "explanation": "Paying down $2500.00 of revolving balance would add about 45 points.",
 "factor_deltas": [{"key": "utilization", "change": 45}, ...]}
```

## Debt

| Method | Path | Notes |
|---|---|---|
| `GET POST PATCH DELETE` | `/debt/debts` | `DELETE` archives |
| `GET POST` | `/debt/payments` | |
| `POST` | `/debt/plans/simulate` | Projects without storing |
| `POST` | `/debt/plans/compare` | All strategies against the minimum-only baseline |
| `POST` | `/debt/plans/budget-for-target` | Inverts: budget needed for N months |
| `POST` | `/debt/plans/create` | Stores and activates |
| `GET` | `/debt/plans/active` | |
| `GET` | `/debt/plans` | History — superseded plans are retained |

Strategies: `avalanche`, `snowball`, `optimal`. Omit `monthly_budget` and the
planner suggests one from declared surplus. `include_schedule: true` returns
the full month-by-month projection.

```bash
curl -X POST localhost:8000/api/v1/debt/plans/compare \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"monthly_budget": "650.00"}'
```
```json
{"optimal":      {"months_to_debt_free": 16, "total_interest": "657.45"},
 "avalanche":    {"months_to_debt_free": 16, "total_interest": "719.42"},
 "snowball":     {"months_to_debt_free": 16, "total_interest": "767.45"},
 "minimum_only": {"months_to_debt_free": 80, "total_interest": "7873.70"}}
```

Domain errors specific to this app:

| Code | Status | Meaning |
|---|---|---|
| `no_debts` | 422 | Nothing to plan |
| `insufficient_budget` | 422 | Below the sum of minimums, or interest outruns payment |

## Marketplace

| Method | Path | Notes |
|---|---|---|
| `GET` | `/marketplace/products` | Full catalogue, unfiltered by eligibility |
| `POST` | `/marketplace/match` | Ranked offers; `persist: true` stores them |
| `GET` | `/marketplace/offers` | |
| `POST` | `/marketplace/offers/{id}/click` | Records the click-through |

Each result carries `estimated_apr`, `approval_odds`, `estimated_benefit`,
`rank_score` and a `reasons` list explaining the placement.

## Communications

| Method | Path | Notes |
|---|---|---|
| `GET` | `/comms/messages` | The user's inbox, **including suppressed messages and why** |
| `GET` | `/comms/rules` | Staff-writable |
| `POST` | `/comms/sweep` | Run the pipeline now; returns the remaining weekly budget |

## Analytics

Ingestion is authenticated; **every report is staff-only**, because they
aggregate across all users.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/analytics/touchpoints` | Marketing touch |
| `POST` | `/analytics/events` | Funnel step |
| `GET POST` | `/analytics/spend` | Staff only |
| `GET POST` | `/analytics/subscriptions` | |
| `GET` | `/analytics/reports/channels` | CAC, activation, revenue, ROAS |
| `GET` | `/analytics/reports/retention?months=12` | Cohort retention |
| `GET` | `/analytics/reports/funnel?channel=` | Step conversion |
| `GET` | `/analytics/reports/attribution` | First vs last vs linear touch |
| `GET` | `/analytics/reports/payback` | Months to repay CAC |

## Operational

| Method | Path | Notes |
|---|---|---|
| `GET` | `/healthz` | Liveness — touches no dependency |
| `GET` | `/readyz` | Readiness — checks the database, 503 when degraded |
