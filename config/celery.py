"""
Celery application.

Queues are declared here so Phase 5 can route message sending and webhook
processing separately: a slow burst of outbound sends must never delay the
processing of inbound Meta status callbacks.
"""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("reachbuzz")

# All Celery settings live in Django settings under the CELERY_ prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

app.conf.task_default_queue = "default"
app.conf.task_routes = {
    "whatsapp.tasks.send_message": {"queue": "whatsapp_send"},
    "whatsapp.tasks.dispatch_campaign": {"queue": "whatsapp_send"},
    "whatsapp.tasks.simulate_status_callbacks": {"queue": "whatsapp_send"},
    "whatsapp.tasks.process_webhook_event": {"queue": "whatsapp_webhook"},
    "whatsapp.tasks.process_pending_webhooks": {"queue": "whatsapp_webhook"},
}

# Scheduled campaigns are launched by a periodic sweep rather than a timer per
# campaign, so a worker restart cannot lose a pending schedule.
app.conf.beat_schedule = {
    "run-due-campaigns": {
        "task": "whatsapp.tasks.run_due_campaigns",
        "schedule": crontab(minute="*"),
        "options": {"queue": "default", "expires": 55},
    },
    # The webhook endpoint stores a payload and answers 200 before queueing the
    # work, so a broker blip between those two steps would strand a delivery
    # report. This notices anything left behind.
    "process-pending-webhooks": {
        "task": "whatsapp.tasks.process_pending_webhooks",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "whatsapp_webhook", "expires": 240},
    },
    # A monthly message quota is measured from current_period_start, so nothing
    # resets until the period does. Hourly rather than daily: a customer whose
    # period ends at 09:00 should not wait until midnight to send again.
    "roll-billing-periods": {
        "task": "billing.tasks.roll_billing_periods",
        "schedule": crontab(minute=5),
        "options": {"queue": "default", "expires": 3300},
    },
    # Daily, not hourly. Retrying a declined card every hour annoys the customer
    # and, on some networks, counts against the merchant.
    "collect-due-invoices": {
        "task": "billing.tasks.collect_due_invoices",
        "schedule": crontab(hour=9, minute=30),
        "options": {"queue": "default", "expires": 3600},
    },
}

app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> str:
    """Trivial task used to verify that the worker and broker are connected."""
    return f"celery ok: {self.request.id}"
