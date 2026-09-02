"""
Brute-force protection for the sign-in form.

The REST login has been throttled since Phase 2 (``throttle_scope = "login"``).
The HTML form was not, which meant the rate limit could be walked around simply
by posting to the page a browser uses. Two doors, one lock. This is the second
lock.

**Counted per client address, never per account.** Locking an account after N
failures would hand anyone who knows an operator's email address the ability to
lock them out of their own system, which trades a brute-force risk for a
denial-of-service one. Throttling the source of the attempts costs the attacker
their attempts and costs the legitimate user nothing, unless they happen to be
behind the same address — which is why the block expires on its own rather than
needing an administrator to clear it.

**The counter is not the audit trail.** Every failure is still recorded by
``accounts.signals``; this only decides when to stop checking passwords.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache

from core.audit import client_ip

logger = logging.getLogger(__name__)

CACHE_PREFIX = "login-attempts"

DEFAULT_LIMIT = 10
DEFAULT_WINDOW_SECONDS = 300


def _limit() -> int:
    return getattr(settings, "LOGIN_ATTEMPT_LIMIT", DEFAULT_LIMIT)


def _window() -> int:
    return getattr(settings, "LOGIN_ATTEMPT_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)


def _key(request) -> str:
    return f"{CACHE_PREFIX}:{client_ip(request) or 'unknown'}"


def failure_count(request) -> int:
    return cache.get(_key(request), 0)


def is_locked_out(request) -> bool:
    """Whether this client has spent its attempts for the current window."""
    if _limit() <= 0:  # 0 disables the throttle entirely
        return False
    return failure_count(request) >= _limit()


def record_failure(request) -> int:
    """
    Count one failed attempt, and return the new total.

    ``add`` then ``incr`` rather than ``set``: the window has to start at the
    first failure and expire on its own schedule. Rewriting the value on every
    attempt would refresh the timeout each time, so a slow attacker would never
    be released and a legitimate user would be locked out for as long as the
    attacker kept trying.
    """
    key = _key(request)
    cache.add(key, 0, _window())
    try:
        return cache.incr(key)
    except ValueError:
        # The entry expired between the add and the incr. One lost attempt is
        # the right way to lose this race.
        cache.set(key, 1, _window())
        return 1


def reset(request) -> None:
    """Clear the counter after a successful sign-in."""
    cache.delete(_key(request))


def lockout_message() -> str:
    """
    Deliberately says nothing about the account.

    A message that varied by whether the email existed would turn the lockout
    into a user-enumeration oracle — the exact thing the sign-in form's
    identical wrong-password and unknown-email responses avoid.
    """
    minutes = max(1, round(_window() / 60))
    return (
        f"Too many sign-in attempts. Please wait {minutes} minute"
        f"{'s' if minutes != 1 else ''} and try again."
    )
