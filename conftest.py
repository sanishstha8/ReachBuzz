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
        # Verified unless a test says otherwise. Sending is gated on a
        # confirmed address, and almost no test is about that gate — leaving it
        # false by default would make every launch test fail for a reason it
        # was not written to check. Pass email_verified=False to exercise it.
        extra.setdefault("email_verified", True)
        return User.objects.create_user(email=email, password=TEST_PASSWORD, role=role, **extra)

    return _make_user


@pytest.fixture
def operator(make_user):
    return make_user("operator@example.com", UserRole.OPERATOR)


@pytest.fixture
def viewer(make_user, organization):
    """A read-only user, inside the same tenant as everybody else."""
    return _join(organization, make_user("viewer@example.com", UserRole.VIEWER))


@pytest.fixture
def administrator(make_user, organization):
    """
    An administrator of the *organization*, not of the platform.

    Membership matters as much as the role: a user outside every organization
    resolves to no tenant, so their writes have nowhere to go — correct
    behaviour, but not what a role-permission test means to exercise.
    """
    return _join(organization, make_user("admin@example.com", UserRole.ADMINISTRATOR))


def _join(organization, user, role=None):
    from organizations.models import OrganizationMember, OrganizationRole

    OrganizationMember.objects.get_or_create(
        organization=organization,
        user=user,
        defaults={"role": role or OrganizationRole.ADMIN},
    )
    return user


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser(email="root@example.com", password=TEST_PASSWORD)


@pytest.fixture
def organization(db, operator):
    """
    The organization the test operator belongs to.

    Nearly every fixture below hangs off this, so a test that creates a contact
    and a test that signs in are talking about the same tenant. Tests that care
    about isolation create a second one explicitly — see `other_organization`.
    """
    from organizations.models import Organization, OrganizationMember, OrganizationRole

    org = Organization.objects.create(name="Test Organization", owner=operator)
    OrganizationMember.objects.create(
        organization=org, user=operator, role=OrganizationRole.OWNER
    )
    return org


@pytest.fixture
def other_organization(db, make_user):
    """A second tenant, for proving one customer cannot reach another's data."""
    from organizations.models import Organization, OrganizationMember, OrganizationRole

    outsider = make_user("outsider@example.com")
    org = Organization.objects.create(name="Other Organization", owner=outsider)
    OrganizationMember.objects.create(
        organization=org, user=outsider, role=OrganizationRole.OWNER
    )
    org.outsider = outsider
    return org


@pytest.fixture
def auth_client(operator, organization) -> Client:
    """
    Django test client signed in as an operator, inside an organization.

    The organization is not optional: views scope every query to the caller's
    tenant, so a signed-in user without one sees nothing. Depending on it here
    means the ordinary test does not have to think about tenancy, while the
    isolation tests still create a second organization explicitly.
    """
    client = Client()
    client.force_login(operator)
    return client


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def auth_api_client(operator, organization) -> APIClient:
    client = APIClient()
    client.force_login(operator)
    return client


@pytest.fixture
def make_contact(db, organization):
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

        extra.setdefault("organization", organization)
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
def make_template(db, organization):
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
        extra.setdefault("organization", organization)
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
def make_campaign(db, operator, organization):
    """Factory for draft campaigns."""
    from campaigns.models import Campaign

    counter = {"n": 0}

    def _make_campaign(name: str | None = None, **extra):
        counter["n"] += 1
        extra.setdefault("organization", organization)
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


# ---------------------------------------------------------------------------
# The network
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def http():
    """
    No test may reach the network. Every outbound HTTP call must be declared.

    The project's rule is that the suite passes without Meta credentials and
    without a network, and once a provider makes real HTTP calls that rule
    stops enforcing itself: a test that forgot to stub a request would quietly
    contact Meta, pass or fail on someone's connection, and leak whatever
    token happened to be in the environment.

    Unregistered requests raise instead. Tests that expect a call register it::

        def test_send(http):
            http.add(responses.POST, url, json={...}, status=200)

    This covers ``requests``, which is the only HTTP client in the project.
    """
    import responses

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture(autouse=True)
def _clean_cache():
    """
    Every test starts with an empty cache.

    The cache now holds state that decides behaviour — the sign-in throttle's
    failure counter and the broker health probe — and it is process-wide, not
    per-test. Without this, a test that exhausts the login limit would lock out
    whichever test happened to run next, and only in that ordering.
    """
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
