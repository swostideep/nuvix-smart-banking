# ADR-0004 — Partner payout is excluded from offer ranking

**Status:** accepted

## Context

NuviX earns a payout when a user funds a loan or opens a card through a
partner. That payout varies by partner and is a real part of the business
model. The marketplace ranks offers for the user.

## Decision

`Partner.payout_per_funded` is stored, reported and **absent from the ranking
function**. Offers are ordered by expected value *to the user*:
`estimated_benefit × approval_odds`.

There is a test asserting that a 40× increase in a partner's payout does not
reorder a single offer.

## Reasoning

Ranking by revenue is the mechanism that turns a recommendation engine into an
ad network. It is also invisible to the user, gradual, and individually
defensible at every step — which is what makes it worth deciding once, in
writing, rather than case by case.

The product premise is that the ranking is on the customer's side. That
premise does not survive a revenue term in the sort key, and neither does the
subscription business that depends on it.

Commercial terms legitimately affect **which partners are in the catalogue**.
They do not affect the order within it.

## Consequences

**Good.** The ranking function is defensible to a user, a partner and a
regulator. The test makes the property regression-proof rather than a matter
of ongoing discipline.

**Bad.** Some revenue is left on the table. That is the intended trade.

**Related.** For the same reason, approval odds are never rounded up to
certainty. Implying a user will be approved when they are marginal costs them
a hard inquiry — a real harm, to benefit the marketplace.
