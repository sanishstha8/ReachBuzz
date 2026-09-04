"""
The payment webhook endpoint.

Unauthenticated and CSRF-exempt, which makes it the most exposed surface in the
application, and the one where being wrong costs money rather than accuracy.
Three things are tested harder than anything else:

* an unsigned request writes **nothing**;
* a redelivered notification credits **once**;
* a hostile header produces a 403, not a 500.

The third is not hypothetical. This project has already had that bug once, in
the WhatsApp webhook, where ``hmac.compare_digest`` raised TypeError on a
non-ASCII header and turned a rejection into a server error.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from billing import payments
from billing.invoicing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    PaymentWebhookEvent,
    PaymentWebhookStatus,
)
from billing.providers.base import PaymentEvent
from billing.providers.mock_provider import MockPaymentProvider

pytestmark = pytest.mark.django_db

URL = "/api/billing/webhook/"


def signed(client, payload: dict, *, secret_ok: bool = True, signature: str | None = None):
    body = json.dumps(payload).encode()
    if signature is None:
        signature = MockPaymentProvider().signature_for(body)
        if not secret_ok:
            signature = "0" * 64
    return client.post(
        URL, data=body, content_type="application/json", HTTP_X_MOCK_SIGNATURE=signature
    )


@pytest.fixture
def charged(organization, on_plan, make_plan):
    """An issued invoice with a pending charge the provider will confirm."""
    plan = make_plan("priced", price=Decimal("29.00"), currency="USD")
    subscription = on_plan(organization, plan)
    invoice = payments.issue(payments.generate_invoice(subscription))
    Payment.objects.create(
        invoice=invoice,
        provider="mock",
        provider_reference="ref_abc",
        idempotency_key=f"{invoice.number}:1",
        amount=invoice.total,
        currency=invoice.currency,
        status=PaymentStatus.PENDING,
    )
    return invoice


def success_payload(reference="ref_abc", event_id="evt_1"):
    return {
        "id": event_id,
        "type": PaymentEvent.SUCCEEDED,
        "reference": reference,
        "amount": "29.00",
        "currency": "USD",
    }


class TestSignatureIsTheAuthentication:
    def test_an_unsigned_request_is_refused(self, client) -> None:
        response = client.post(URL, data="{}", content_type="application/json")

        assert response.status_code == 403
        assert PaymentWebhookEvent.objects.count() == 0

    def test_a_wrong_signature_writes_nothing(self, client) -> None:
        """
        The endpoint is public. Anything that persists on unverified input is a
        way for a stranger to fill the database — and here, to forge a payment.
        """
        response = signed(client, success_payload(), secret_ok=False)

        assert response.status_code == 403
        assert PaymentWebhookEvent.objects.count() == 0

    def test_a_non_ascii_signature_is_a_403_not_a_500(self, client) -> None:
        """
        compare_digest raises TypeError on non-ASCII str. Comparing bytes is
        what keeps an attacker-supplied header from becoming a server error.
        """
        response = signed(client, success_payload(), signature="Ω" * 20)

        assert response.status_code == 403

    def test_a_valid_signature_is_accepted(self, client, charged) -> None:
        assert signed(client, success_payload()).status_code == 200

    def test_get_is_refused(self, client) -> None:
        """No handshake here. A 200 to an unauthenticated GET advertises the door."""
        assert client.get(URL).status_code == 403

    def test_an_oversized_body_is_refused_unread(self, client) -> None:
        payload = {"id": "e", "type": PaymentEvent.SUCCEEDED, "reference": "r", "pad": "x" * 600_000}

        assert signed(client, payload).status_code == 403


class TestItStoresAndDefers:
    def test_a_valid_delivery_is_stored(self, client, charged) -> None:
        signed(client, success_payload())

        event = PaymentWebhookEvent.objects.get()
        assert event.signature_valid is True
        assert event.event_id == "evt_1"

    def test_the_payload_is_kept_whole(self, client, charged) -> None:
        """The provider's own words are the evidence; our reading of them is not."""
        signed(client, success_payload())

        assert PaymentWebhookEvent.objects.get().payload["amount"] == "29.00"

    def test_an_unparseable_type_is_ignored_not_stored(self, client) -> None:
        response = signed(client, {"id": "x", "type": "something.else", "reference": "r"})

        assert response.status_code == 200
        assert PaymentWebhookEvent.objects.count() == 0

    def test_signed_but_invalid_json_does_not_500(self, client) -> None:
        body = b"{not json"
        response = client.post(
            URL,
            data=body,
            content_type="application/json",
            HTTP_X_MOCK_SIGNATURE=MockPaymentProvider().signature_for(body),
        )

        assert response.status_code == 200
        assert PaymentWebhookEvent.objects.count() == 0


class TestReplaysCreditOnce:
    def test_the_same_notification_twice_is_stored_once(self, client, charged) -> None:
        signed(client, success_payload())
        signed(client, success_payload())

        assert PaymentWebhookEvent.objects.count() == 1

    def test_the_replay_still_answers_200(self, client, charged) -> None:
        """A non-200 asks the provider to try again, which helps nobody."""
        signed(client, success_payload())

        response = signed(client, success_payload())

        assert response.status_code == 200
        assert response.json()["queued"] == 0

    def test_the_invoice_is_credited_once(self, client, charged, settings) -> None:
        settings.CELERY_TASK_ALWAYS_EAGER = True
        from billing.tasks import process_payment_webhook

        signed(client, success_payload())
        signed(client, success_payload())

        for event in PaymentWebhookEvent.objects.all():
            process_payment_webhook(str(event.pk))
            process_payment_webhook(str(event.pk))  # and the task itself replayed

        charged.refresh_from_db()
        assert charged.amount_paid == Decimal("29.00")
        assert charged.status == InvoiceStatus.PAID

    def test_two_different_events_for_one_charge_are_both_stored(
        self, client, charged
    ) -> None:
        """One charge legitimately produces several notifications."""
        signed(client, success_payload(event_id="evt_1"))
        signed(client, {**success_payload(event_id="evt_2"), "type": PaymentEvent.REFUNDED})

        assert PaymentWebhookEvent.objects.count() == 2


class TestTheProcessingTask:
    def test_it_marks_the_event_processed(self, client, charged) -> None:
        from billing.tasks import process_payment_webhook

        signed(client, success_payload())
        event = PaymentWebhookEvent.objects.get()

        process_payment_webhook(str(event.pk))

        event.refresh_from_db()
        assert event.status == PaymentWebhookStatus.PROCESSED
        assert event.processed_at is not None

    def test_an_event_that_changes_nothing_is_marked_ignored(self, client, charged) -> None:
        """Distinguishable from processed, so a real backlog stays visible."""
        from billing.tasks import process_payment_webhook

        signed(client, success_payload(reference="unknown_ref"))
        event = PaymentWebhookEvent.objects.get()

        process_payment_webhook(str(event.pk))

        event.refresh_from_db()
        assert event.status == PaymentWebhookStatus.IGNORED

    def test_a_vanished_event_does_not_raise(self) -> None:
        import uuid

        from billing.tasks import process_payment_webhook

        assert process_payment_webhook(str(uuid.uuid4())) == "missing"


class TestCollectionSweep:
    def test_it_collects_what_is_overdue(self, organization, on_plan, make_plan) -> None:
        from django.utils import timezone

        from billing.tasks import collect_due_invoices

        plan = make_plan("priced", price=Decimal("29.00"), currency="USD")
        invoice = payments.issue(payments.generate_invoice(on_plan(organization, plan)))
        Invoice.objects.filter(pk=invoice.pk).update(
            due_at=timezone.now() - timezone.timedelta(days=1)
        )

        result = collect_due_invoices()

        invoice.refresh_from_db()
        assert result["settled"] == 1
        assert invoice.status == InvoiceStatus.PAID

    def test_it_leaves_invoices_that_are_not_due_yet(
        self, organization, on_plan, make_plan
    ) -> None:
        from billing.tasks import collect_due_invoices

        plan = make_plan("priced", price=Decimal("29.00"), currency="USD")
        invoice = payments.issue(payments.generate_invoice(on_plan(organization, plan)))

        collect_due_invoices()

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.OPEN

    def test_running_it_twice_charges_once(self, organization, on_plan, make_plan) -> None:
        from django.utils import timezone

        from billing.tasks import collect_due_invoices

        plan = make_plan("priced", price=Decimal("29.00"), currency="USD")
        invoice = payments.issue(payments.generate_invoice(on_plan(organization, plan)))
        Invoice.objects.filter(pk=invoice.pk).update(
            due_at=timezone.now() - timezone.timedelta(days=1)
        )

        collect_due_invoices()
        collect_due_invoices()

        assert invoice.payments.count() == 1
