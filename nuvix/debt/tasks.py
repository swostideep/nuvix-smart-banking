"""Background work for the debt engine."""

from __future__ import annotations

import logging

from celery import shared_task

from nuvix.debt.models import PayoffPlan
from nuvix.debt.services import planner

logger = logging.getLogger(__name__)


@shared_task(name="nuvix.debt.tasks.recompute_active_plans")
def recompute_active_plans(limit: int = 500) -> dict[str, int]:
    """Refresh every active plan against current balances.

    Balances move daily; a plan computed in January is wrong by March. Running
    this nightly keeps the debt-free date on the customer's dashboard honest,
    and a date that quietly slips is exactly the thing worth telling them
    about -- which is what the communications engine picks up from here.
    """

    refreshed = failed = 0
    plans = PayoffPlan.objects.filter(is_active=True).select_related("user")[:limit]
    for plan in plans:
        try:
            planner.save_plan(plan.user, plan.strategy, plan.monthly_budget)
            refreshed += 1
        except Exception:
            failed += 1
            logger.warning(
                "plan_recompute_failed", extra={"user_id": str(plan.user_id)}, exc_info=True
            )
    return {"refreshed": refreshed, "failed": failed}
