"""
Rate limits for the unauthenticated doors.

Everything here counts events **per client address, never per account.** Locking
an account after N attempts would hand anyone who knows an operator's email
address the ability to lock them out of their own system, which trades a
brute-force risk for a denial-of-service one. Throttling the source costs the
attacker their attempts and costs the legitimate user nothing, unless they
happen to be behind the same address â which is why every block expires on its
own rather than needing an administrator to clear it.

**The counter is not the audit trail.** Failures are still recorded by
``accounts.signals``; these only decide when to stop doing the work.

Three doors use it:

``login``
    Brute-force protection for the sign-in *form*. The REST login has been
    throttled since Phase 2 (``throttle_scope = "login"``); the form was not,
    which meant the limit could be walked around by posting to the page a
    browser uses. Two doors, one lock.

``signup``
    Registration creates rows and sends mail to an address the *caller* supplies,
    which makes it two things at once: a way to fill the database, and a way to
    send our mail to somebody who never asked for it. Counted on success, not on
    failure â a rejected form costs nothing, an account created does.

``outbound_email``
    Password reset and re-sending a confirmation link. Both mail an address on
    demand, so both are a way to bury somebody's inbox using our mail server and
    our reputation.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache

from core.audit import client_ip

logger = logging.getLogger(__name__)


class RateLimit:
    """
    N events per rolling window, keyed by client address.

    One class rather than three copies of the same cache arithmetic: the
    ``add``-then-``incr`` sequence below is subtle enough that having it in one
    place is worth more than the indirection costs.
    """

    def __init__(
        self,
        prefix: str,
        *,
        limit_setting: str,
        window_setting: str,
        default_limit: int,
        default_window: int,
        message: str,
    ) -> None:
        self.prefix = prefix
        self.limit_setting = limit_setting
        self.window_setting = window_setting
        self.default_limit = default_limit
        self.default_window = default_window
        self.message_template = message

    # -- configuration ----------------------------------------------------

    @property
    def limit(self) -> int:
        return getattr(settings, self.limit_setting, self.default_limit)

    @property
    def window(self) -> int:
        return getattr(settings, self.window_setting, self.default_window)

    def _key(self, request) -> str:
        return f"{self.prefix}:{client_ip(request) or 'unknown'}"

    # -- counting ---------------------------------------------------------

    def count(self, request) -> int:
        return cache.get(self._key(request), 0)

    def is_exceeded(self, request) -> bool:
        """Whether this client has spent its allowance for the current window."""
        if self.limit <= 0:  # 0 disables the limit entirely
            return False
        return self.count(request) >= self.limit

    def record(self, request) -> int:
        """
        Count one event, and return the new total.

        ``add`` then ``incr`` rather than ``set``: the window has to start at the
        first event and expire on its own schedule. Rewriting the value every
        time would refresh the timeout, so a slow attacker would never be
        released and a legitimate user would stay blocked for as long as the
        attacker kept going.
        """
        key = self._key(request)
        cache.add(key, 0, self.window)
        try:
            return cache.incr(key)
        except ValueError:
            # The entry expired between the add and the incr. One lost event is
            # the right way to lose this race.
            cache.set(key, 1, self.window)
            return 1

    def reset(self, request) -> None:
        cache.delete(self._key(request))

    def message(self) -> str:
        """
        Deliberately says nothing about any account.

        A message that varied by whether the address existed would turn the
        block into a user-enumeration oracle â the exact thing the sign-in
        form's identical wrong-password and unknown-email responses avoid.
        """
        minutes = max(1, round(self.window / 60))
        return self.message_template.format(
            minutes=minutes, plural="s" if minutes != 1 else ""
        )


login = RateLimit(
    "login-attempts",
    limit_setting="LOGIN_ATTEMPT_LIMIT",
    window_setting="LOGIN_ATTEMPT_WINDOW_SECONDS",
    default_limit=10,
    default_window=300,
    message="Too many sign-in attempts. Please wait {minutes} minute{plural} and try again.",
)

signup = RateLimit(
    "signup-attempts",
    limit_setting="SIGNUP_LIMIT",
    window_setting="SIGNUP_WINDOW_SECONDS",
    default_limit=5,
    default_window=3600,
    message=(
        "Too many accounts have been created from this network. "
        "Please wait {minutes} minute{plural} and try again."
    ),
)

outbound_email = RateLimit(
    "outbound-email",
    limit_setting="OUTBOUND_EMAIL_LIMIT",
    window_setting="OUTBOUND_EMAIL_WINDOW_SECONDS",
    default_limit=5,
    default_window=900,
    message=(
        "Too many emails have been requested from this network. "
        "Please wait {minutes} minute{plural} and try again."
    ),
)


# ---------------------------------------------------------------------------
# The sign-in form's original module-level API, kept as-is.
# ---------------------------------------------------------------------------

CACHE_PREFIX = login.prefix
DEFAULT_LIMIT = login.default_limit
DEFAULT_WINDOW_SECONDS = login.default_window


def failure_count(request) -> int:
    return login.count(request)


def is_locked_out(request) -> bool:
    return login.is_exceeded(request)


def record_failure(request) -> int:
    return login.record(request)


def reset(request) -> None:
    login.reset(request)


def lockout_message() -> str:
    return login.message()
