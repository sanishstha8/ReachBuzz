"""
Outbound rate limiting.

This throttles **our own** sending so we stay comfortably inside the messaging
limits of the connected WhatsApp Business Account. It is not, and must not
become, a way to work around a provider limit: when the provider says 429 we
back off and honour ``Retry-After``.

A token bucket in Redis keeps the limit shared across every worker process —
per-process throttling would let N workers send N times the configured rate.
The refill and take are done in one Lua script so two workers cannot both see
the last token.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

# Refill and consume atomically.
#   KEYS[1] bucket key
#   ARGV[1] refill rate (tokens/second)   ARGV[2] bucket capacity
#   ARGV[3] now (seconds, float)          ARGV[4] tokens requested
# Returns {allowed (0|1), wait_seconds}
BUCKET_SCRIPT_LUA = """
local key      = KEYS[1]
local rate     = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local want     = tonumber(ARGV[4])

local bucket   = redis.call('HMGET', key, 'tokens', 'updated')
local tokens   = tonumber(bucket[1])
local updated  = tonumber(bucket[2])

if tokens == nil then
  tokens = capacity
  updated = now
end

local elapsed = math.max(0, now - updated)
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local wait = 0

if tokens >= want then
  tokens = tokens - want
  allowed = 1
else
  wait = (want - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'updated', now)
redis.call('EXPIRE', key, math.ceil(capacity / rate) + 60)

return {allowed, tostring(wait)}
"""


@dataclass(frozen=True)
class Acquisition:
    """Whether a send may proceed, and how long to wait if not."""

    allowed: bool
    wait_seconds: float = 0.0

    @property
    def retry_after(self) -> int:
        """Whole seconds to wait, never zero when a wait is needed."""
        return max(1, int(self.wait_seconds + 0.999))


class RateLimiter(ABC):
    @abstractmethod
    def acquire(self, tokens: int = 1) -> Acquisition:
        """Try to take ``tokens`` from the bucket."""


class NullRateLimiter(RateLimiter):
    """No limiting. Used when the rate is unset, and in tests."""

    def acquire(self, tokens: int = 1) -> Acquisition:
        return Acquisition(allowed=True)


class RedisTokenBucket(RateLimiter):
    """Token bucket shared by every worker through Redis."""

    def __init__(self, rate_per_second: float, *, key: str = "whatsapp:send-rate", client=None):
        self.rate = float(rate_per_second)
        self.capacity = max(1.0, float(rate_per_second))
        self.key = key
        self._client = client
        self._script = None

    @property
    def client(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                getattr(settings, "REDIS_URL", "redis://localhost:6379/0")
            )
        return self._client

    def acquire(self, tokens: int = 1) -> Acquisition:
        try:
            if self._script is None:
                self._script = self.client.register_script(BUCKET_SCRIPT_LUA)

            allowed, wait = self._script(
                keys=[self.key], args=[self.rate, self.capacity, time.time(), tokens]
            )
        except Exception:
            # A rate limiter that is itself broken must not stop the campaign.
            # Failing open is the right call here because the provider applies
            # its own limits regardless, and we honour its Retry-After.
            logger.exception("Rate limiter unavailable; allowing the send through")
            return Acquisition(allowed=True)

        return Acquisition(
            allowed=bool(int(allowed)),
            wait_seconds=float(wait.decode() if isinstance(wait, bytes) else wait),
        )


def get_rate_limiter() -> RateLimiter:
    """
    Build the configured limiter.

    ``WHATSAPP_SEND_RATE_PER_SECOND=0`` disables limiting, which is what the
    test settings use so the suite needs no Redis.
    """
    rate = getattr(settings, "WHATSAPP_SEND_RATE_PER_SECOND", 0) or 0

    if rate <= 0:
        return NullRateLimiter()
    if getattr(settings, "WHATSAPP_RATE_LIMIT_BACKEND", "redis") == "null":
        return NullRateLimiter()

    return RedisTokenBucket(rate)
