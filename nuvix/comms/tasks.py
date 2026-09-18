"""Scheduled communications work."""

from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from nuvix.accounts.models import User
from nuvix.comms.services.dispatcher import dispatch_due, sweep_user

logger = logging.getLogger(__name__)


@shared_task(name="nuvix.comms.tasks.sweep_user")
def sweep_single_user(user_id: str) -> dict[str, int]:
    user = User.objects.filter(id=user_id, is_active=True).first()
    if user is None:
        return {"evaluated": 0, "scheduled": 0, "suppressed": 0}
    return sweep_user(user)


@shared_task(name="nuvix.comms.tasks.run_daily_sweep")
def run_daily_sweep(batch_size: int = 500) -> dict[str, int]:
    """Fan the sweep out over active users.

    One task per user rather than one task for everybody: a single long task
    holding a transaction across thousands of users would block, and one
    user's failure would roll back everyone else's messages.
    """

    totals = {"users": 0, "dispatched": 0}
    user_ids = User.objects.filter(is_active=True).values_list("id", flat=True)[:batch_size]
    for user_id in user_ids:
        sweep_single_user.delay(str(user_id))
        totals["users"] += 1
    logger.info("daily_sweep_enqueued", extra={"users": totals["users"]})
    return totals


@shared_task(name="nuvix.comms.tasks.dispatch_due_messages")
def dispatch_due_messages(limit: int = 500) -> dict[str, int]:
    return dispatch_due(now=timezone.now(), limit=limit)
