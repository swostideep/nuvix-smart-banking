"""Scheduled credit work."""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task
from django.utils import timezone

from nuvix.credit.models import CreditProfile
from nuvix.credit.services import scoring

logger = logging.getLogger(__name__)

#: How stale a score may get before it is recomputed.
STALE_AFTER_DAYS = 7


@shared_task(name="nuvix.credit.tasks.refresh_stale_credit_profiles")
def refresh_stale_credit_profiles(limit: int = 1000) -> dict[str, int]:
    """Rescore profiles that have not been touched recently.

    Batched and capped so the nightly job has a bounded runtime; anything not
    reached tonight is simply the most stale batch tomorrow.
    """

    cutoff = timezone.now() - dt.timedelta(days=STALE_AFTER_DAYS)
    stale = (
        CreditProfile.objects.filter(last_scored_at__lt=cutoff)
        .select_related("user")
        .order_by("last_scored_at")[:limit]
    )

    rescored = failed = 0
    for profile in stale:
        try:
            scoring.derive_from_linked_accounts(profile.user)
            scoring.rescore(profile.user)
            rescored += 1
        except Exception:
            failed += 1
            logger.warning(
                "rescore_failed", extra={"user_id": str(profile.user_id)}, exc_info=True
            )
    return {"rescored": rescored, "failed": failed}
