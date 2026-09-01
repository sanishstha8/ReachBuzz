"""
Runtime health of the sending pipeline.

The dashboard needs to tell an operator the truth about whether a campaign can
actually be sent right now. "A dispatcher is registered" is not that truth — the
Celery sender is registered at startup whether or not Redis is running. This
module answers the harder question by actually opening a connection.

Results are cached briefly so a down broker costs one timeout per window rather
than one per page view.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_KEY = "whatsapp:broker-health"
CACHE_SECONDS = 30
CONNECT_TIMEOUT = 1.5


@dataclass(frozen=True)
class BrokerHealth:
    reachable: bool
    detail: str = ""

    @property
    def label(self) -> str:
        return "reachable" if self.reachable else "unreachable"


def check_broker(*, use_cache: bool = True) -> BrokerHealth:
    """Whether the Celery broker can be reached."""
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return BrokerHealth(True, "Tasks run inline; no broker required.")

    if use_cache:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return BrokerHealth(*cached)

    health = _probe()
    if use_cache:
        cache.set(CACHE_KEY, (health.reachable, health.detail), CACHE_SECONDS)
    return health


def _probe() -> BrokerHealth:
    try:
        from config.celery import app

        connection = app.connection()
        connection.ensure_connection(max_retries=0, timeout=CONNECT_TIMEOUT)
        connection.release()
    except Exception as exc:
        # The class name is safe to show; the URL may embed a password.
        logger.warning("Broker probe failed: %s", exc.__class__.__name__)
        return BrokerHealth(
            False,
            "Could not connect. Check that Redis is running and CELERY_BROKER_URL is correct.",
        )

    return BrokerHealth(True, "Connected.")


def pipeline_status() -> dict[str, object]:
    """A single summary of everything that decides whether sending works."""
    from campaigns import dispatch
    from whatsapp.services.factory import is_simulated, provider_name
    from whatsapp.services.rate_limiter import get_rate_limiter

    broker = check_broker()
    limiter = get_rate_limiter()

    return {
        "provider": provider_name(),
        "is_simulated": is_simulated(),
        "dispatcher_registered": dispatch.is_sending_available(),
        "broker_reachable": broker.reachable,
        "broker_detail": broker.detail,
        "rate_limiter": limiter.__class__.__name__,
        "send_rate_per_second": getattr(settings, "WHATSAPP_SEND_RATE_PER_SECOND", 0),
        "can_send": dispatch.is_sending_available() and broker.reachable,
    }
