"""Celery application.

Long-running work -- nightly credit pulls, payoff-plan recomputation and the
communications sweep -- runs here rather than inside a request/response cycle.
"""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("nuvix")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "refresh-credit-profiles": {
        "task": "nuvix.credit.tasks.refresh_stale_credit_profiles",
        "schedule": crontab(hour=3, minute=0),
    },
    "recompute-payoff-plans": {
        "task": "nuvix.debt.tasks.recompute_active_plans",
        "schedule": crontab(hour=4, minute=0),
    },
    "run-communications-sweep": {
        "task": "nuvix.comms.tasks.run_daily_sweep",
        "schedule": crontab(hour=14, minute=0),
    },
    "dispatch-scheduled-messages": {
        "task": "nuvix.comms.tasks.dispatch_due_messages",
        "schedule": crontab(minute="*/15"),
    },
}
