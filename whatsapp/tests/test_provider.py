"""The provider abstraction and the mock implementation."""

from __future__ import annotations

import pytest
from django.test import override_settings

from core.exceptions import ProviderNotConfigured
from whatsapp.services.base import SendResult, WhatsAppProvider
from whatsapp.services.factory import get_provider, is_simulated, provider_name
from whatsapp.services.meta_cloud_api import MetaWhatsAppProvider
from whatsapp.services.mock_provider import MockWhatsAppProvider


class TestFactory:
    def test_returns_the_mock_provider_by_default(self) -> None:
        provider = get_provider()
        assert isinstance(provider, MockWhatsAppProvider)
        assert provider.is_simulated is True

    @override_settings(WHATSAPP_PROVIDER="meta")
    def test_returns_the_meta_provider_when_configured(self) -> None:
        """One setting swaps the implementation; no application code changes."""
        provider = get_provider()
        assert isinstance(provider, MetaWhatsAppProvider)
        assert provider.is_simulated is False

    def test_unknown_provider_is_rejected_by_name(self) -> None:
        with pytest.raises(ProviderNotConfigured, match="Unknown WHATSAPP_PROVIDER"):
            get_provider("carrier-pigeon")

    def test_explicit_name_overrides_the_setting(self) -> None:
        assert isinstance(get_provider("meta"), MetaWhatsAppProvider)

    @override_settings(WHATSAPP_PROVIDER="meta")
    def test_is_simulated_tracks_the_provider(self) -> None:
        assert provider_name() == "meta"
        assert is_simulated() is False

    def test_both_providers_implement_the_contract(self) -> None:
        for provider in (MockWhatsAppProvider(), MetaWhatsAppProvider()):
            assert isinstance(provider, WhatsAppProvider)


class TestSendResult:
    def test_ok_helper(self) -> None:
        result = SendResult.ok("wamid.1")
        assert result.success is True
        assert result.provider_message_id == "wamid.1"
        assert result.retryable is False

    def test_failure_helper(self) -> None:
        result = SendResult.failure("429", "Too many", retryable=True, retry_after=7)
        assert result.success is False
        assert result.retryable is True
        assert result.retry_after == 7


class TestMockProvider:
    def test_successful_send_returns_a_provider_id(self) -> None:
        result = MockWhatsAppProvider(failure_rate=0.0).send_template(
            to="+9779800000000", template_name="order_ready", language="en_US"
        )

        assert result.success is True
        assert result.provider_message_id.startswith("wamid.MOCK.")

    def test_each_send_gets_a_distinct_id(self) -> None:
        provider = MockWhatsAppProvider(failure_rate=0.0)
        first = provider.send_text(to="+9779800000000", body="hi")
        second = provider.send_text(to="+9779800000000", body="hi")

        assert first.provider_message_id != second.provider_message_id

    def test_invalid_number_fails_permanently(self) -> None:
        """A real provider would reject it too, so the mock must not fake success."""
        result = MockWhatsAppProvider(failure_rate=0.0).send_text(to="12345", body="hi")

        assert result.success is False
        assert result.retryable is False
        assert result.error_code == "mock_invalid_number"

    def test_failure_rate_of_one_always_fails(self) -> None:
        result = MockWhatsAppProvider(failure_rate=1.0, seed=1).send_text(
            to="+9779800000000", body="hi"
        )
        assert result.success is False

    def test_failure_rate_of_zero_never_fails(self) -> None:
        provider = MockWhatsAppProvider(failure_rate=0.0, seed=1)
        for _ in range(50):
            assert provider.send_text(to="+9779800000000", body="hi").success

    def test_simulated_failures_include_both_retryable_and_permanent(self) -> None:
        """Both retry branches must be reachable locally, or they never run."""
        provider = MockWhatsAppProvider(failure_rate=1.0, seed=7)
        outcomes = {
            provider.send_text(to="+9779800000000", body="x").retryable for _ in range(40)
        }
        assert outcomes == {True, False}

    def test_seeded_provider_is_deterministic(self) -> None:
        a = MockWhatsAppProvider(failure_rate=0.5, seed=42)
        b = MockWhatsAppProvider(failure_rate=0.5, seed=42)
        assert [a.send_text(to="+9779800000000", body="x").success for _ in range(10)] == [
            b.send_text(to="+9779800000000", body="x").success for _ in range(10)
        ]

    def test_fetch_templates_returns_nothing_rather_than_pretending(self) -> None:
        assert MockWhatsAppProvider().fetch_templates() == []

    @override_settings(MOCK_PROVIDER_FAILURE_RATE=1.0)
    def test_reads_its_failure_rate_from_settings(self) -> None:
        assert MockWhatsAppProvider().send_text(to="+9779800000000", body="x").success is False

    def test_does_not_log_the_full_phone_number(self, caplog) -> None:
        """Numbers are personal data; only the tail belongs in a log line."""
        with caplog.at_level("INFO", logger="whatsapp.services.mock_provider"):
            MockWhatsAppProvider(failure_rate=0.0).send_text(to="+9779812345678", body="x")

        assert "+9779812345678" not in caplog.text
        assert "…5678" in caplog.text


class TestMetaProviderGuards:
    """Configuration must fail by name, and never by leaking a credential."""

    @override_settings(META_API_VERSION="", META_ACCESS_TOKEN="", META_PHONE_NUMBER_ID="")
    def test_missing_configuration_is_reported_by_name(self) -> None:
        with pytest.raises(ProviderNotConfigured) as exc_info:
            MetaWhatsAppProvider().check_configuration()

        message = str(exc_info.value)
        assert "META_ACCESS_TOKEN" in message
        assert "META_API_VERSION" in message

    @override_settings(META_ACCESS_TOKEN="EAAsupersecrettoken1234567890")
    def test_configuration_error_never_prints_a_credential(self) -> None:
        with override_settings(META_API_VERSION=""):
            with pytest.raises(ProviderNotConfigured) as exc_info:
                MetaWhatsAppProvider().check_configuration()

        assert "EAAsupersecrettoken1234567890" not in str(exc_info.value)

    def test_configuration_is_checked_before_anything_else(self) -> None:
        """Missing credentials must surface as that, not as 'not implemented'."""
        with pytest.raises(ProviderNotConfigured):
            MetaWhatsAppProvider().send_text(to="+9779800000000", body="hi")

    @override_settings(
        META_API_VERSION="vTEST",
        META_ACCESS_TOKEN="configured-for-this-test",
        META_PHONE_NUMBER_ID="123",
        META_WABA_ID="456",
    )
    def test_template_sync_needs_more_than_the_send_credentials(self, http) -> None:
        """The WABA id is only needed for templates, so it is checked separately."""
        import responses

        http.add(
            responses.GET,
            "https://graph.facebook.com/vTEST/456/message_templates",
            json={"data": []},
            status=200,
        )
        assert MetaWhatsAppProvider().fetch_templates() == []

    @override_settings(
        META_API_VERSION="vTEST",
        META_ACCESS_TOKEN="configured-for-this-test",
        META_PHONE_NUMBER_ID="123",
        META_WABA_ID="",
    )
    def test_template_sync_needs_the_waba_id(self) -> None:
        with pytest.raises(ProviderNotConfigured, match="META_WABA_ID"):
            MetaWhatsAppProvider().fetch_templates()
