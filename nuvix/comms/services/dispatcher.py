"""The communications sweep.

For one user, in order:

1. **Evaluate** every active rule's trigger. Most return nothing.
2. **Deduplicate.** Each candidate gets a stable key derived from the user,
   the rule and the salient facts. A unique constraint on that key makes the
   sweep idempotent -- running it twice in a day cannot double-send.
3. **Cool down.** A rule that fired within its cooldown is suppressed even if
   its trigger still holds; a condition being true is not a reason to say so
   again.
4. **Select.** Candidates compete for a small weekly budget, so the highest
   priority wins -- pulled off a heap rather than by sorting the whole list,
   because most sweeps discard most candidates.
5. **Rate limit.** A token bucket per user, across all rules.
6. **Schedule.** Never inside quiet hours.

Suppressed candidates are written to the database with their reason. Knowing
what was withheld is how an over-eager rule gets found.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import heapq
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from nuvix.accounts.models import User
from nuvix.comms.models import (
    CommunicationRule,
    Message,
    MessageStatus,
)
from nuvix.comms.services import triggers
from nuvix.comms.services.channels import DeliveryError, backend_for
from nuvix.comms.services.ratelimit import TokenBucket, consume

logger = logging.getLogger(__name__)

SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60


def weekly_bucket() -> TokenBucket:
    """One bucket per user, sized by the platform-wide weekly cap."""

    return TokenBucket(
        capacity=float(settings.NUVIX["COMMS_MAX_PER_WEEK"]),
        period_seconds=SEVEN_DAYS_SECONDS,
    )


def dedup_key(user: User, rule: CommunicationRule, context: dict) -> str:
    """Stable identity for "this message, about these facts, for this user".

    The context is hashed, not just the rule, so that a genuinely new fact --
    a different shortfall date, a different milestone -- is a new message,
    while a re-run over unchanged facts is not. Keys are sorted so that dict
    ordering cannot make two identical contexts hash differently.
    """

    payload = json.dumps(context, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{user.id}|{rule.key}|{payload}".encode()).hexdigest()[:32]
    return f"{rule.key}:{digest}"


def in_cooldown(user: User, rule: CommunicationRule, now: dt.datetime) -> bool:
    if rule.cooldown_days <= 0:
        return False
    since = now - dt.timedelta(days=rule.cooldown_days)
    return Message.objects.filter(
        user=user, rule=rule, status=MessageStatus.SENT, sent_at__gte=since
    ).exists()


def exceeded_rule_cap(user: User, rule: CommunicationRule, now: dt.datetime) -> bool:
    since = now - dt.timedelta(days=7)
    sent = Message.objects.filter(
        user=user, rule=rule, status=MessageStatus.SENT, sent_at__gte=since
    ).count()
    return sent >= rule.max_per_week


def next_send_time(now: dt.datetime) -> dt.datetime:
    """Push a send out of quiet hours.

    A balance alert at 3am reads as an emergency. Quiet hours are configured
    as ``(start, end)`` in local hours and wrap across midnight.
    """

    start, end = settings.NUVIX["COMMS_QUIET_HOURS"]
    hour = timezone.localtime(now).hour

    quiet = hour >= start or hour < end
    if not quiet:
        return now

    local = timezone.localtime(now)
    target = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if local.hour >= start:
        target += dt.timedelta(days=1)
    return target


def collect_candidates(user: User, now: dt.datetime) -> list[dict]:
    """Evaluate every active rule and return the ones with something to say."""

    candidates = []
    rules = CommunicationRule.objects.filter(is_active=True).select_related("template")

    for rule in rules:
        if not rule.template.is_active:
            continue
        context = triggers.evaluate(rule.trigger, user, rule.parameters)
        if context is None:
            continue
        candidates.append(
            {
                "rule": rule,
                "context": context,
                "dedup_key": dedup_key(user, rule, context),
            }
        )
    return candidates


@transaction.atomic
def sweep_user(user: User, now: dt.datetime | None = None) -> dict[str, int]:
    """Run the full pipeline for one user."""

    now = now or timezone.now()
    candidates = collect_candidates(user, now)

    stats = {"evaluated": len(candidates), "scheduled": 0, "suppressed": 0}
    if not candidates:
        return stats

    # Max-heap by priority (negated, since heapq is a min-heap). Popping
    # lazily matters because the weekly cap usually stops the loop long before
    # the list is exhausted, so most candidates are never fully considered.
    heap = [(-item["rule"].priority, index, item) for index, item in enumerate(candidates)]
    heapq.heapify(heap)

    bucket = weekly_bucket()

    while heap:
        _, _, item = heapq.heappop(heap)
        rule: CommunicationRule = item["rule"]
        context: dict = item["context"]
        key: str = item["dedup_key"]

        if Message.objects.filter(dedup_key=key).exists():
            stats["suppressed"] += 1
            continue

        reason = ""
        if in_cooldown(user, rule, now):
            reason = "cooldown"
        elif exceeded_rule_cap(user, rule, now):
            reason = "rule_weekly_cap"
        else:
            allowed, _ = consume("comms", str(user.id), bucket, now=now.timestamp())
            if not allowed:
                reason = "user_weekly_cap"

        subject, body = rule.template.render(context)
        try:
            Message.objects.create(
                user=user,
                rule=rule,
                channel=rule.template.channel,
                subject=subject,
                body=body,
                status=MessageStatus.SUPPRESSED if reason else MessageStatus.SCHEDULED,
                suppression_reason=reason,
                scheduled_for=next_send_time(now),
                dedup_key=key,
                context=context,
            )
        except IntegrityError:
            # Another worker created the same message between the check above
            # and this insert. The constraint did its job; nothing to do.
            stats["suppressed"] += 1
            continue

        if reason:
            stats["suppressed"] += 1
            if reason == "user_weekly_cap":
                # The budget is spent; everything still queued would be
                # suppressed for the same reason, so stop evaluating.
                break
        else:
            stats["scheduled"] += 1

    return stats


def deliver(message: Message) -> bool:
    """Hand one scheduled message to its channel backend."""

    backend = backend_for(message.channel)
    try:
        backend.send(message)
    except DeliveryError:
        message.status = MessageStatus.FAILED
        message.save(update_fields=["status", "updated_at"])
        logger.warning(
            "message_delivery_failed",
            extra={"message_id": str(message.id), "channel": message.channel},
            exc_info=True,
        )
        return False

    message.status = MessageStatus.SENT
    message.sent_at = timezone.now()
    message.save(update_fields=["status", "sent_at", "updated_at"])
    return True


def dispatch_due(now: dt.datetime | None = None, limit: int = 500) -> dict[str, int]:
    """Deliver every message whose scheduled time has arrived."""

    now = now or timezone.now()
    due = Message.objects.filter(
        status=MessageStatus.SCHEDULED, scheduled_for__lte=now
    ).select_related("user")[:limit]

    sent = failed = 0
    for message in due:
        if deliver(message):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed}
