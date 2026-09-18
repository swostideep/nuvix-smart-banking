# Algorithms

Every non-trivial algorithm in NuviX, with its complexity and — more
importantly — the reason it is that algorithm and not the obvious one.

---

## 1. Payoff optimisation

**`nuvix/debt/services/strategies.py`**

### The problem

Given debts $(b_i, r_i, m_i)$ — balance, APR, contractual minimum — and a
fixed monthly budget $B \ge \sum m_i$, choose how to allocate the surplus each
month to minimise total interest paid.

### Why avalanche is not the answer

With constant rates it is. A dollar applied to the highest-rate balance
removes the most future interest, and an exchange argument shows any other
order can be improved by swapping. That is the textbook result, and
`compare_strategies` reproduces it.

The assumption fails on the most common debt structure in this market: a
balance transfer at **0% for nine months, reverting to 26.99%**. Greedy on
*today's* rate ranks that balance last and leaves it untouched until the cliff
arrives, at which point the full balance starts compounding at the standard
rate.

### The optimiser

Two families of candidate, both scored on the exact simulator:

**Dynamic** — the named strategies, re-sorting live debts every month.
Avalanche is always in the running, so the result can never be worse than it.

**Static** — one priority order held for the life of the plan. Seeded from
three informed orderings (avalanche order, snowball order, and an order ranked
by `horizon_rate`, which averages each debt's effective APR over the next 18
months so a lapsing promotion prices near its revert rate). The best seed is
then refined by **steepest-descent local search over adjacent
transpositions**: try each adjacent swap, take the first strict improvement,
repeat.

Adjacent transpositions are the right neighbourhood because the objective is
close to monotone in the ordering — swapping two debts far apart in priority
almost never helps unless the intermediate swaps help first.

### Complexity

| | |
|---|---|
| One simulation | $O(m \cdot n \log n)$ — $m$ months, $n$ debts |
| Optimiser | $\le$ `MAX_EVALUATIONS` (120) simulations, memoised on the ordering |
| Exhaustive alternative | $n!$ simulations |

### Honesty about what it returns

A **local** optimum over static orderings, raced against the dynamic
strategies. Not a proven global optimum. That is stated in the docstring
rather than glossed, and the plan is always reported with the interest it
actually costs.

### Verification

`test_finds_the_brute_force_optimum` runs exhaustive search over every
permutation and asserts the optimiser matches it. `test_never_worse_than_any_named_strategy`
asserts the guarantee across three budgets.

Measured on a three-debt book with one promotional balance, budget $650:

| Strategy | Months | Interest |
|---|---:|---:|
| Snowball | 16 | $767.45 |
| Avalanche | 16 | $719.42 |
| **Optimised** | **16** | **$657.45** |
| Minimum only | 80 | $7,873.70 |

---

## 2. Amortisation simulator

**`nuvix/debt/services/amortization.py`** — $O(m \cdot n \log n)$

One simulator scores every strategy, so a difference in output is a real
difference in the plan rather than a difference in arithmetic.

Each month, in the order an issuer applies them:

1. **Interest accrues** on the opening balance at that month's effective rate.
2. **Contractual minimums** are paid on every live debt.
3. **The surplus cascades** down the priority order, and any payment that would
   overshoot a balance is trimmed and passed on — the effect that makes each
   payoff accelerate the next.

The re-prioritisation inside the loop cannot be hoisted out: a promotion
expiring, or a debt clearing, legitimately changes the order mid-plan.

### Two guards

**Budget below minimums** → `InsufficientBudgetError` with the shortfall in
`details`.

**Negative amortisation** — the budget covers the minimums but interest still
outruns payment, so balances grow. This is the more insidious failure; without
a guard the loop runs to `MAX_MONTHS` and hands the customer a nonsense date.
It raises by default, and is *permitted* for the minimum-only baseline, where
"this never pays off" is the most persuasive thing the comparison can say.

That distinction produced a real bug. When the baseline never amortises it
stops early, so its accumulated interest is a partial sum — and
`baseline − plan` reported a **negative saving** for exactly the users in the
worst position. Both figures are now withheld behind a
`baseline_never_pays_off` flag.

### Money arithmetic

`Decimal` quantised to cents throughout; floats are banned. A schedule
iterates hundreds of times and binary drift compounds into visibly wrong
figures.

Monthly rate is `APR / 12`, **not** $(1+r)^{1/12}-1$ — that is how US issuers
compute a periodic rate on a statement. Matching the issuer's arithmetic
matters more than theoretical purity.

---

## 3. Budget for a target date

**`budget_for_target_months`** — $O(\log(\text{range}) \cdot m \cdot n)$

Inverts the question: *"what must I pay monthly to be debt-free in 18
months?"*

Months-to-payoff is monotonically non-increasing in the budget, so the answer
is a **binary search on dollars** — about 20 simulations across a $0–$50k
window, against tens of thousands for a linear scan. The invariant is
maintained explicitly: `low` fails, `high` succeeds, narrow to the dollar.

`test_result_is_tight` asserts that two dollars less misses the target, so the
search cannot silently return a loose bound.

---

## 4. Recurring-transaction detection

**`nuvix/banking/services/recurrence.py`** — $O(n \log n)$

Bank feeds do not label a paycheque or a car payment, so NuviX derives them.

1. **Normalise the merchant.** `"POS DEBIT SAFEWAY #1423 SEATTLE WA 03/14"`
   and `"SAFEWAY #0881 PORTLAND OR"` both collapse to `"SAFEWAY"` by stripping
   noise tokens and long digit runs and keeping two tokens — enough to
   disambiguate `"STATE FARM"` without splitting one merchant across branches.
2. **Bucket by (merchant, sign).** A refund from a merchant you also pay is a
   different phenomenon; merging them destroys both the cadence and the mean.
3. **Classify the date gaps.** Each gap maps to the *closest* cadence within
   tolerance, so a 15-day gap resolves to semimonthly rather than being
   claimed by biweekly's upper bound. A majority (≥60%) must agree.
4. **Check amount stability** using **median absolute deviation over the
   median** — robust, so one unusually large electricity bill in a heatwave
   does not disqualify an obvious monthly series.
5. **Score confidence** as `agreement × stability × evidence`, three
   independent signals each in $[0,1]$, so the product stays interpretable.

A deterministic statistical rule rather than a learned model, deliberately:
the output drives customer-facing statements ("your paycheque lands Friday")
and a rule can be explained to a support agent and to a regulator.

Monthly and quarterly cadences step by **calendar month, clamped to the last
valid day**, so rent on the 31st does not drift backwards through the year.

---

## 5. Cashflow projection and safe-to-spend

**`nuvix/banking/services/cashflow.py`** — $O(k \log s + h)$

Each recurring series is an already-sorted generator of dates, so the streams
are merged with a **heap** rather than concatenated and sorted.

The output that matters is not the closing balance but the **trough**:

> `safe_to_spend` = (lowest projected balance before the next income event) − buffer

Money that will be needed before more arrives is not spendable, however
healthy today's balance looks. The $50 buffer exists because a projection is
an estimate, and telling someone they can spend to exactly zero converts any
small error into an overdraft fee — the precise harm the product exists to
prevent.

An overdue series is pulled forward to the window start rather than dropped: a
late bill is still owed, and hiding it overstates safe-to-spend.

---

## 6. Credit factor model

**`nuvix/credit/engine/factors.py`** — $O(1)$

Additive over five factors at the bureaus' published weights, mapping to
300–850:

$$\text{score} = 300 + 550 \sum_i w_i \cdot s_i$$

Additive and transparent **by constraint**: US fair-lending rules require
specific reason codes on an adverse decision, and a gradient-boosted ensemble
cannot produce them honestly.

Curve shapes that are not arbitrary:

- **Payment history squares the on-time rate.** 95% on-time sounds excellent
  and is roughly one missed payment a year, which bureaus treat far more
  harshly than the linear reading suggests.
- **Utilisation is stepped, not linear** — because scoring models are. The
  gap between 29% and 31% is worth far more than between 60% and 62%. This is
  why an $85 payment at the right moment can be worth 40 points, and it is
  what the utilisation nudge is built on.
- **One maxed card caps the factor** even when the aggregate looks healthy.
- **Age is log-scaled, saturating at ten years** — the first two years matter
  far more than the eleventh, and it keeps a thin file feeling improvable
  rather than hopeless.
- **A file with no history scores zero on payment history.** An early version
  awarded full marks for a record that did not exist, giving a brand-new
  consumer 624 — and every downstream decision built on that number was wrong.

Property-based-in-spirit tests assert **monotonicity** on every factor: lower
utilisation never lowers the score, more late payments never raise it, and so
on. Those catch sign errors that spot-checks miss.

---

## 7. What-if simulation

**`nuvix/credit/engine/simulator.py`** — $O(1)$ per action

Each action is a transformation on `CreditInputs`; the score is then
**recomputed by the same factor model**. No separate estimate that can drift
out of agreement with the model it is simulating.

It models the trade-offs people get wrong: opening a card adds limit (helps
utilisation) but costs an inquiry and lowers average age — frequently net
negative in the short run, which is exactly why it is worth simulating before
acting. Closing the oldest card is modelled explicitly as the most common
self-inflicted score wound.

`rank_actions` simulates every realistic option against *this* profile and
ranks by points actually earned, so the advice panel is specific rather than
generic.

---

## 8. Token-bucket rate limiting

**`nuvix/comms/services/ratelimit.py`** — $O(1)$

Capacity $C$ over period $T$, refilling $C/T$ per second.

**Why not a fixed window:** a user capped at four messages a week can receive
four on Sunday night and four on Monday morning. The bucket refills
continuously, so the long-run rate holds wherever the boundary falls, while
still allowing a small burst when something urgent coincides with a nudge.
`test_no_burst_across_a_window_boundary` asserts exactly this.

The pure bucket is clock-injectable and fully unit tested. `consume()` is the
only function touching shared state, and it does so through the cache as a
read-modify-write — **knowingly racy**. The cost of the race is one extra
message; the cost of avoiding it is a distributed lock on the hot path of
every send. A hard cap would need a Redis Lua script doing check-and-decrement
atomically. That trade is documented at the call site rather than discovered
later.

Capacity 0 — the legitimate way to disable a channel — once divided by zero.
It now reports an infinite wait.

---

## 9. Communications selection

**`nuvix/comms/services/dispatcher.py`** — $O(r \log r)$ for $r$ rules

1. **Evaluate** every active rule's trigger. Most return nothing.
2. **Deduplicate** by `sha256(user, rule, sorted(context))`. A unique
   constraint on that key makes the sweep **idempotent** — running it twice in
   a day cannot double-send — while a genuinely new fact produces a new key.
3. **Cool down.** A condition still being true is not a reason to say so again.
4. **Select** on a **max-heap**. Popping lazily matters because the weekly cap
   usually stops the loop long before the list is exhausted.
5. **Meter** through the token bucket.
6. **Schedule** outside quiet hours — a balance alert at 3am reads as an
   emergency.

Suppressed candidates are written to the database *with their reason*. Knowing
what was withheld is the only way to find an over-eager rule.

A trigger reaches into the credit, debt, banking and marketplace engines. One
failing for one user must not abort the sweep for everyone else, so
`evaluate()` converts any exception into "nothing to say" and logs it with
enough context to fix.

---

## 10. Offer matching and ranking

**`nuvix/marketplace/services/matching.py`** — $O(n \log n)$ over eligible products

**Eligibility** is a hard SQL filter. A product whose box the user does not fit
is not an offer; showing it produces a decline, and a decline costs a hard
inquiry.

**Pricing** interpolates linearly within the product's advertised APR band over
a 300-point range above its floor. A borrower at the floor gets the ceiling
rate — which is what actually happens.

**Approval odds** are a logistic on how far inside the box the user sits, on
the two criteria that drive most declines, combined as a **geometric mean**:

$$P = \sqrt{\sigma(k_s \cdot \Delta_{\text{score}}) \cdot \sigma(k_d \cdot \Delta_{\text{DTI}})} \times \text{partner rate}$$

The geometric mean makes the *weaker* criterion dominate — a product is not
"half approvable" because the user is excellent on score and hopeless on DTI.
Sitting exactly on the minimum is a coin flip, not a yes.

**Benefit** is measured against what the user pays *today*, not against zero.
A 19% loan is a terrible product in the abstract and an excellent one for
someone servicing 27% cards. `test_benefit_is_measured_against_the_current_rate_not_zero`
asserts the sign flips with the comparison point.

**Rank** is `benefit × approval_odds` — expected value to the user. Multiplying
by the odds is what stops an unreachable headline rate outranking an
attainable good one.

**Partner payout is excluded from the ranking function** and there is a test
asserting a 40× payout increase does not reorder a single offer. Ranking by
revenue is what turns a recommendation engine into an ad network.

---

## 11. SQL portability

**`nuvix/analytics/sql/dialect.py`**

The analytics are raw SQL by choice — cohort retention, payback and
attribution want window functions, `ROW_NUMBER` partitions and self-joins, and
through an ORM they get longer, slower and harder to check.

Raw SQL costs portability, because date functions are the least standardised
part of SQL. The dialect shim is that cost paid once. Three hard-won rules
live in it:

**Fragments reference columns, never placeholders.** `months_between` expands
its argument twice, so a `%s` passed in is emitted twice and bound once.
Queries needing a parameter inside one bind it as a column in a preceding CTE.

**No percent signs in emitted SQL.** The obvious `strftime('%Y', …)` is a
trap: Django's SQLite backend renders debug queries with `sql % params`, so
`%Y` raises `unsupported format character` — with `DEBUG=True` only. It passes
CI and breaks the moment a developer opens the report locally. SQLite stores
dates as `YYYY-MM-DD` text, so `substr` gets the same answer with no `%`.

**Both arguments to `safe_divide` are parenthesised.** Without it,
`SUM(a) + SUM(b)` over a denominator binds the division to `SUM(b)` alone. The
query still runs and returns a plausible number — ROAS reported **1535.20**
where the true ratio was **0.56**. A wrong number that looks right is the
worst way for a reporting bug to behave.

All three have regression tests.

---

## 12. Funnel monotonicity

**`nuvix/analytics/queries.py::funnel_conversion`**

Counts **distinct users**, not events, so a step-to-step rate above 100% is
impossible — the usual symptom of counting events.

Each stage counts users who reached it *or anything after it*, computed as a
backwards running maximum. Reaching a later step implies passing the earlier
ones, so this is the correct definition — and without it, an
un-instrumented step reads as zero and the *next* step appears to gain users,
producing negative drop-offs and >100% conversion.

`users_recorded` is reported alongside so a gap in instrumentation stays
visible rather than being papered over.
