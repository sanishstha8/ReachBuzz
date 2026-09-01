"""Project-wide pytest fixtures."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework.test import APIClient

from accounts.models import UserRole

User = get_user_model()

TEST_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def password() -> str:
    return TEST_PASSWORD


@pytest.fixture
def make_user(db):
    """Factory for users with an explicit role."""

    def _make_user(email: str = "operator@example.com", role: str = UserRole.OPERATOR, **extra):
        extra.setdefault("first_name", "Test")
        extra.setdefault("last_name", "User")
        return User.objects.create_user(email=email, password=TEST_PASSWORD, role=role, **extra)

    return _make_user


@pytest.fixture
def operator(make_user):
    return make_user("operator@example.com", UserRole.OPERATOR)


@pytest.fixture
def viewer(make_user):
    return make_user("viewer@example.com", UserRole.VIEWER)


@pytest.fixture
def administrator(make_user):
    return make_user("admin@example.com", UserRole.ADMINISTRATOR)


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser(email="root@example.com", password=TEST_PASSWORD)


@pytest.fixture
def auth_client(operator) -> Client:
    """Django test client signed in as an operator."""
    client = Client()
    client.force_login(operator)
    return client


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def auth_api_client(operator) -> APIClient:
    client = APIClient()
    client.force_login(operator)
    return client


@pytest.fixture
def make_contact(db):
    """
    Factory producing contacts with explicit consent state.

    Lives at the project root because the dashboard and campaign tests need an
    audience too, not just the contacts app.
    """
    from contacts.models import Contact, ContactStatus, OptInSource

    counter = {"n": 0}

    def _make_contact(
        name: str = "Test Contact",
        phone_number: str | None = None,
        *,
        opted_in: bool = False,
        status: str = ContactStatus.ACTIVE,
        **extra,
    ):
        counter["n"] += 1
        if phone_number is None:
            phone_number = f"+97798{counter['n']:08d}"

        contact = Contact(
            name=name,
            phone_number=phone_number,
            country_code="977",
            status=status,
            **extra,
        )
        if opted_in:
            contact.opt_in(OptInSource.MANUAL)
        contact.save()
        return contact

    return _make_contact


# ---------------------------------------------------------------------------
# Templates, campaigns and the dispatcher seam
# ---------------------------------------------------------------------------


@pytest.fixture
def make_template(db):
    """Factory for message templates, local by default."""
    from whatsapp.models import MessageTemplate, TemplateSource, TemplateStatus

    counter = {"n": 0}

    def _make_template(
        name: str | None = None,
        body_text: str = "Hello {{name}}, your order {{order_id}} is ready.",
        *,
        source: str = TemplateSource.LOCAL,
        status: str = TemplateStatus.NOT_SUBMITTED,
        **extra,
    ):
        counter["n"] += 1
        return MessageTemplate.objects.create(
            name=name or f"template_{counter['n']}",
            body_text=body_text,
            source=source,
            status=status,
            **extra,
        )

    return _make_template


@pytest.fixture
def approved_template(make_template):
    """A template Meta has approved — usable under any provider."""
    from whatsapp.models import TemplateSource, TemplateStatus

    return make_template(
        "order_ready",
        source=TemplateSource.SYNCED,
        status=TemplateStatus.APPROVED,
        provider_template_id="1234567890",
    )


@pytest.fixture
def local_template(make_template):
    """A development-only template — refused under the live provider."""
    return make_template("local_promo", "Hi {{name}}, here is {{offer}}.")


@pytest.fixture
def make_campaign(db, operator):
    """Factory for draft campaigns."""
    from campaigns.models import Campaign

    counter = {"n": 0}

    def _make_campaign(name: str | None = None, **extra):
        counter["n"] += 1
        return Campaign.objects.create(
            name=name or f"Campaign {counter['n']}", created_by=operator, **extra
        )

    return _make_campaign


@pytest.fixture
def recording_dispatcher():
    """
    Install a stub sender for the duration of a test.

    Phase 5 registers the real Celery dispatcher; until then this lets the full
    launch path — validation, materialization, state transition — be exercised
    without a broker. It records the campaigns it was handed.
    """
    from campaigns import dispatch

    calls: list = []

    def _dispatcher(campaign) -> int:
        calls.append(campaign)
        return campaign.messages.count()

    dispatch.register_dispatcher(_dispatcher)
    _dispatcher.calls = calls
    try:
        yield _dispatcher
    finally:
        dispatch.clear_dispatcher()


@pytest.fixture(autouse=True)
def _no_dispatcher_by_default():
    """
    No sender is registered unless a test asks for one.

    This keeps the "sending unavailable" path honest and stops one test's
    dispatcher leaking into another's.
    """
    from campaigns import dispatch

    dispatch.clear_dispatcher()
    yield
    dispatch.clear_dispatcher()
