"""
Pipeline health.

The dashboard tells an operator whether a campaign can be sent right now. That
claim has to be true, so the probe gets tested rather than assumed.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings

from whatsapp import health


@pytest.fixture(autouse=True)
def _clear_health_cache():
    cache.delete(health.CACHE_KEY)
    yield
    cache.delete(health.CACHE_KEY)


class TestCheckBroker:
    def test_eager_mode_needs_no_broker(self) -> None:
        result = health.check_broker()
        assert result.reachable is True
        assert "inline" in result.detail

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_reports_reachable_when_the_connection_opens(self) -> None:
        connection = MagicMock()
        with patch("config.celery.app.connection", return_value=connection):
            result = health.check_broker(use_cache=False)

        assert result.reachable is True
        assert result.label == "reachable"
        connection.release.assert_called_once()

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_reports_unreachable_when_the_connection_fails(self) -> None:
        with patch("config.celery.app.connection", side_effect=OSError("refused")):
            result = health.check_broker(use_cache=False)

        assert result.reachable is False
        assert result.label == "unreachable"
        assert "Redis" in result.detail

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_the_failure_detail_never_leaks_the_broker_url(self) -> None:
        """A broker URL can embed a password; the class name is enough."""
        with override_settings(CELERY_BROKER_URL="redis://:hunter2@localhost:6379/0"):
            with patch("config.celery.app.connection", side_effect=OSError("refused")):
                result = health.check_broker(use_cache=False)

        assert "hunter2" not in result.detail

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_the_result_is_cached_so_a_down_broker_costs_one_timeout(self) -> None:
        probe = patch("config.celery.app.connection", side_effect=OSError("refused"))

        with probe as mocked:
            health.check_broker()
            health.check_broker()
            health.check_broker()

        assert mocked.call_count == 1

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_use_cache_false_always_probes(self) -> None:
        with patch("config.celery.app.connection", side_effect=OSError("x")) as mocked:
            health.check_broker(use_cache=False)
            health.check_broker(use_cache=False)

        assert mocked.call_count == 2


class TestPipelineStatus:
    def test_can_send_requires_a_dispatcher(self) -> None:
        """No dispatcher registered: the answer must be no."""
        status = health.pipeline_status()

        assert status["dispatcher_registered"] is False
        assert status["can_send"] is False

    def test_can_send_with_a_dispatcher_and_a_live_broker(self, recording_dispatcher) -> None:
        status = health.pipeline_status()

        assert status["dispatcher_registered"] is True
        assert status["broker_reachable"] is True
        assert status["can_send"] is True

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_a_dead_broker_blocks_sending_despite_a_dispatcher(
        self, recording_dispatcher
    ) -> None:
        with patch("config.celery.app.connection", side_effect=OSError("refused")):
            status = health.pipeline_status()

        assert status["dispatcher_registered"] is True
        assert status["broker_reachable"] is False
        assert status["can_send"] is False

    def test_reports_the_active_provider(self) -> None:
        status = health.pipeline_status()
        assert status["provider"] == "mock"
        assert status["is_simulated"] is True

    @override_settings(WHATSAPP_PROVIDER="meta")
    def test_live_provider_is_not_reported_as_simulated(self) -> None:
        assert health.pipeline_status()["is_simulated"] is False

    def test_reports_the_rate_limiter_in_use(self) -> None:
        # The test settings disable limiting, so this must not claim otherwise.
        assert health.pipeline_status()["rate_limiter"] == "NullRateLimiter"


class TestPipelineStatusCommand:
    def test_reports_that_sending_is_blocked(self) -> None:
        out = StringIO()
        call_command("pipeline_status", stdout=out)
        output = out.getvalue()

        assert "Provider" in output
        assert "mock" in output
        assert "NOT registered" in output
        assert "cannot be launched" in output

    def test_reports_that_sending_is_available(self, recording_dispatcher) -> None:
        out = StringIO()
        call_command("pipeline_status", stdout=out)
        output = out.getvalue()

        assert "registered" in output
        assert "reachable" in output
        assert "Campaigns can be launched." in output

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_prints_the_broker_detail_when_unreachable(self, recording_dispatcher) -> None:
        out = StringIO()
        with patch("config.celery.app.connection", side_effect=OSError("refused")):
            call_command("pipeline_status", stdout=out)

        assert "unreachable" in out.getvalue()
        assert "Redis" in out.getvalue()

    def test_never_prints_a_credential(self) -> None:
        out = StringIO()
        with override_settings(META_ACCESS_TOKEN="EAAsupersecrettoken123456"):
            call_command("pipeline_status", stdout=out)

        assert "EAAsupersecrettoken123456" not in out.getvalue()
