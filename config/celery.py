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

app = Celery("whatsapp_bulk_messaging")

# All Celery settings live in Django settings under the CELERY_ prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

app.conf.task_default_queue = "default"
app.conf.task_routes = {
    "whatsapp.tasks.send_message": {"queue": "whatsapp_send"},
    "whatsapp.tasks.dispatch_campaign": {"queue": "whatsapp_send"},
    "whatsapp.tasks.simulate_status_callbacks": {"queue": "whatsapp_send"},
    "whatsapp.tasks.process_webhook_event": {"queue": "whatsapp_webhook"},
}

# Scheduled campaigns are launched by a periodic sweep rather than a timer per
# campaign, so a worker restart cannot lose a pending schedule.
app.conf.beat_schedule = {
    "run-due-campaigns": {
        "task": "whatsapp.tasks.run_due_campaigns",
        "schedule": crontab(minute="*"),
        "options": {"queue": "default", "expires": 55},
    },
}

app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> str:
    """Trivial task used to verify that the worker and broker are connected."""
    return f"celery ok: {self.request.id}"
