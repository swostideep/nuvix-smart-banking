# ADR-0001 — A modular monolith, not microservices

**Status:** accepted

## Context

NuviX spans seven bounded contexts — identity, banking, credit, debt,
marketplace, communications, analytics. The reflex for that list is one
service each.

## Decision

One Django project with seven apps and a strict layering rule, deployed as a
single image alongside a Celery worker.

## Reasoning

The engines are **not independent**. A communications trigger reads the credit
profile, runs the payoff optimiser, and calls the marketplace matcher — all to
decide whether to send one push notification. As microservices that is three
network hops with three failure modes to answer a question that currently
takes 40ms in-process.

The coupling that microservices are meant to break is *deployment* coupling,
and this codebase does not have a deployment problem. What it needs is
*comprehension* coupling control, and a layering rule gives that at
compile-time cost rather than at network cost:

- engines import no models
- apps talk through services, never through each other's ORM
- shared concerns live in `nuvix.core`

## Consequences

**Good.** One migration history, so a foreign key from `Debt` to
`LinkedAccount` is just a foreign key. One transaction boundary, so saving a
plan and deactivating its predecessor is atomic. The whole suite runs in under
two seconds.

**Bad.** One deploy unit — a bad analytics query can hurt the payoff API. Team
ownership eventually needs process boundaries.

**The migration path is open.** Because engines are pure and services are the
only ORM callers, extracting `analytics` (the most independent, most
read-heavy context) means moving one package and replacing service calls with
HTTP. The layering rule is what keeps that cheap.
