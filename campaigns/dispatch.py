"""
The seam between "a campaign is ready to send" and "something sends it".

Phase 4 builds the plan: audience resolution, validation, and one Message row
per recipient. Phase 5 supplies the worker that drains those rows. Rather than
hard-wiring Celery into the campaign services (which would make them untestable
without a broker) the sender registers itself here.

With no dispatcher registered, :func:`require_dispatcher` raises *before* any
state change, so a campaign is never left sitting in PROCESSING with nothing
able to process it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from core.exceptions import DomainError

logger = logging.getLogger(__name__)


class SendingUnavailable(DomainError):
    """Raised when a launch is attempted with no sender wired up."""

    default_message = (
        "Sending is not available: no message dispatcher is configured. "
        "Start the Celery worker, or check WHATSAPP_PROVIDER."
    )
    status_code = 503
    code = "sending_unavailable"


class Dispatcher(Protocol):
    """What a sender must provide."""

    def __call__(self, campaign) -> int:
        """Queue the campaign's pending messages. Returns how many were queued."""


_dispatcher: Callable[..., int] | None = None


def register_dispatcher(dispatcher: Callable[..., int]) -> None:
    """Install the sender. Phase 5 calls this from the whatsapp app's ready()."""
    global _dispatcher
    _dispatcher = dispatcher
    logger.info("Message dispatcher registered: %s", getattr(dispatcher, "__name__", dispatcher))


def clear_dispatcher() -> None:
    """Remove the sender. Used by tests to assert the unavailable path."""
    global _dispatcher
    _dispatcher = None


def get_dispatcher() -> Callable[..., int] | None:
    return _dispatcher


def is_sending_available() -> bool:
    return _dispatcher is not None


def require_dispatcher() -> Callable[..., int]:
    """Return the dispatcher, or raise :class:`SendingUnavailable`."""
    if _dispatcher is None:
        raise SendingUnavailable()
    return _dispatcher


def preflight() -> None:
    """
    Check the sender can actually accept work, before anything changes state.

    A registered dispatcher is not the same as a reachable queue. The sender may
    expose an optional ``preflight`` attribute — the Celery one pings the broker
    — so that an unreachable broker is reported at launch time instead of
    leaving a campaign in PROCESSING with every message stuck at PENDING.
    """
    dispatcher = require_dispatcher()

    check = getattr(dispatcher, "preflight", None)
    if callable(check):
        check()


def dispatch_campaign(campaign) -> int:
    """Hand the campaign to the registered sender."""
    return require_dispatcher()(campaign)
