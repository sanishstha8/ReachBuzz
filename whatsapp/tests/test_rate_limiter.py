"""
Outbound rate limiting.

This throttles our own sending. It is deliberately *not* a way around a
provider limit — when the provider says 429 the task backs off and honours
``Retry-After``.
"""

from __future__ import annotations

import time

import pytest
from django.test import override_settings

from whatsapp.services.rate_limiter import (
    Acquisition,
    NullRateLimiter,
    RedisTokenBucket,
    get_rate_limiter,
)


class FakeRedis:
    """Minimal stand-in that runs the bucket arithmetic the Lua script does."""

    def __init__(self):
        self.state: dict[str, dict[str, float]] = {}

    def register_script(self, _lua):
        def call(keys, args):
            key = keys[0]
            rate, capacity, now, want = (float(a) for a in args)

            bucket = self.state.get(key)
            tokens = bucket["tokens"] if bucket else capacity
            updated = bucket["updated"] if bucket else now

            tokens = min(capacity, tokens + max(0.0, now - updated) * rate)

            allowed, wait = 0, 0.0
            if tokens >= want:
                tokens -= want
                allowed = 1
            else:
                wait = (want - tokens) / rate

            self.state[key] = {"tokens": tokens, "updated": now}
            return [allowed, str(wait)]

        return call


class TestFactory:
    @override_settings(WHATSAPP_SEND_RATE_PER_SECOND=0)
    def test_a_rate_of_zero_disables_limiting(self) -> None:
        assert isinstance(get_rate_limiter(), NullRateLimiter)

    @override_settings(WHATSAPP_SEND_RATE_PER_SECOND=10, WHATSAPP_RATE_LIMIT_BACKEND="null")
    def test_the_null_backend_disables_limiting(self) -> None:
        assert isinstance(get_rate_limiter(), NullRateLimiter)

    @override_settings(WHATSAPP_SEND_RATE_PER_SECOND=10, WHATSAPP_RATE_LIMIT_BACKEND="redis")
    def test_redis_backend_is_selected_when_a_rate_is_set(self) -> None:
        limiter = get_rate_limiter()
        assert isinstance(limiter, RedisTokenBucket)
        assert limiter.rate == 10


class TestNullLimiter:
    def test_always_allows(self) -> None:
        limiter = NullRateLimiter()
        assert all(limiter.acquire().allowed for _ in range(100))


class TestTokenBucket:
    def test_allows_up_to_the_capacity_then_refuses(self) -> None:
        limiter = RedisTokenBucket(5, client=FakeRedis())

        allowed = [limiter.acquire().allowed for _ in range(8)]

        assert allowed[:5] == [True] * 5
        assert allowed[5:] == [False] * 3

    def test_refuses_with_a_wait_time(self) -> None:
        limiter = RedisTokenBucket(2, client=FakeRedis())
        limiter.acquire()
        limiter.acquire()

        result = limiter.acquire()

        assert result.allowed is False
        assert result.wait_seconds > 0
        assert result.retry_after >= 1

    def test_refills_over_time(self) -> None:
        limiter = RedisTokenBucket(50, client=FakeRedis())
        for _ in range(50):
            limiter.acquire()
        assert limiter.acquire().allowed is False

        time.sleep(0.05)

        assert limiter.acquire().allowed is True

    def test_the_bucket_is_shared_across_limiter_instances(self) -> None:
        """Per-process limiting would let N workers send N times the rate."""
        shared = FakeRedis()
        worker_a = RedisTokenBucket(3, client=shared)
        worker_b = RedisTokenBucket(3, client=shared)

        taken = [worker_a.acquire().allowed, worker_b.acquire().allowed,
                 worker_a.acquire().allowed, worker_b.acquire().allowed]

        assert taken == [True, True, True, False]

    def test_a_broken_limiter_fails_open(self, caplog) -> None:
        """
        A rate limiter that is itself down must not stop the campaign: the
        provider applies its own limits regardless, and we honour Retry-After.
        """

        class BrokenRedis:
            def register_script(self, _lua):
                raise ConnectionError("redis is down")

        limiter = RedisTokenBucket(5, client=BrokenRedis())

        with caplog.at_level("ERROR"):
            result = limiter.acquire()

        assert result.allowed is True
        assert "Rate limiter unavailable" in caplog.text


class TestAcquisition:
    @pytest.mark.parametrize(
        "wait,expected", [(0.0, 1), (0.1, 1), (1.0, 1), (1.4, 2), (4.6, 5)]
    )
    def test_retry_after_rounds_up_and_is_never_zero(self, wait, expected) -> None:
        assert Acquisition(allowed=False, wait_seconds=wait).retry_after == expected
