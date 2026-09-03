"""
The demonstration seeder.

Most of this file is about the two guards. ``seed_demo`` fabricates 120 people
and hands them to the campaign machinery, and the only thing standing between
that and a system wired to a real WhatsApp Business Account is a pair of
setting checks. Those are worth more tests than the data generation is.

The rest covers the promise the command makes about *how* it writes: through
the real services, so consent stays audited and the campaign state machine is
respected. Seed data that took shortcuts around those would not be
representative of the thing it exists to demonstrate.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from campaigns.models import Campaign, CampaignStatus
from contacts.models import Contact, ContactGroup, OptInSource
from core.models import AuditAction, AuditLog
from messaging.models import Message
from whatsapp.models import MessageTemplate

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def development_settings(settings):
    """The conditions the command insists on before it will run at all."""
    settings.WHATSAPP_PROVIDER = "mock"
    settings.DEBUG = True
    return settings


@pytest.fixture
def seeded(superuser):
    call_command("seed_demo", contacts=12, campaigns=3, days=20, verbosity=0)


class TestGuards:
    """
    These are the tests that matter. Everything below them is convenience;
    these are the reason the command is safe to have in the repository.
    """

    def test_it_refuses_to_run_against_a_live_provider(self, settings, superuser) -> None:
        """
        The failure this prevents is fabricated contacts in a system holding
        real Meta credentials — which is to say, messages to invented numbers.
        """
        settings.WHATSAPP_PROVIDER = "meta"

        with pytest.raises(CommandError, match="mock"):
            call_command("seed_demo", verbosity=0)

        assert Contact.objects.count() == 0

    def test_it_refuses_to_run_with_debug_off(self, settings, superuser) -> None:
        settings.DEBUG = False

        with pytest.raises(CommandError, match="DEBUG"):
            call_command("seed_demo", verbosity=0)

        assert Contact.objects.count() == 0

    def test_both_guards_are_checked_independently(self, settings, superuser) -> None:
        """
        Two checks rather than one, because either can be wrong on its own:
        DEBUG gets left on by accident, and a provider setting can change
        without a restart. Neither alone should be enough to proceed.
        """
        settings.WHATSAPP_PROVIDER = "meta"
        settings.DEBUG = True
        with pytest.raises(CommandError):
            call_command("seed_demo", verbosity=0)

        settings.WHATSAPP_PROVIDER = "mock"
        settings.DEBUG = False
        with pytest.raises(CommandError):
            call_command("seed_demo", verbosity=0)

    def test_the_guards_run_before_anything_is_written(self, settings, superuser) -> None:
        settings.WHATSAPP_PROVIDER = "meta"

        with pytest.raises(CommandError):
            call_command("seed_demo", verbosity=0)

        assert Contact.objects.count() == 0
        assert Campaign.objects.count() == 0
        assert MessageTemplate.objects.count() == 0
        assert AuditLog.objects.count() == 0

    def test_it_refuses_when_there_is_no_user_to_own_the_data(self) -> None:
        with pytest.raises(CommandError, match="createsuperuser"):
            call_command("seed_demo", verbosity=0)


class TestSeeding:
    def test_it_creates_contacts_groups_campaigns_and_messages(self, seeded) -> None:
        assert Contact.objects.count() == 12
        assert ContactGroup.objects.count() == 3
        assert Campaign.objects.count() == 3
        assert Message.objects.exists()

    def test_the_audience_is_mixed_rather_than_uniformly_consenting(self, seeded) -> None:
        """
        A demo where everyone has consented would hide the constraint the whole
        application is built around.
        """
        assert Contact.objects.filter(opted_in=True).exists()
        assert Contact.objects.filter(opted_in=False).exists()

    def test_consent_and_status_vary_independently(self, superuser) -> None:
        """
        Derived from one roll, every inactive contact would also be
        un-consented, "eligible" would always equal "opted in", and the demo
        would never show the case the consent rule exists for.
        """
        call_command("seed_demo", contacts=200, campaigns=2, verbosity=0)

        opted_in = Contact.objects.filter(opted_in=True).count()
        eligible = Contact.objects.eligible().count()

        assert opted_in > eligible, "no contact consented but was ineligible"

    def test_consent_is_recorded_with_a_source(self, seeded) -> None:
        for contact in Contact.objects.filter(opted_in=True):
            assert contact.opt_in_source in OptInSource.values
            assert contact.opt_in_at is not None

    def test_consent_goes_through_the_audited_service(self, seeded) -> None:
        """Not a second, unaudited path for writing Contact.opted_in."""
        opted_in = Contact.objects.filter(opted_in=True).count()

        assert AuditLog.objects.filter(action=AuditAction.CONTACT_OPTED_IN).count() == opted_in

    def test_campaigns_reach_a_terminal_state_through_the_state_machine(self, seeded) -> None:
        started = Campaign.objects.exclude(status=CampaignStatus.DRAFT)

        assert started.exists()
        for campaign in started:
            assert campaign.started_at is not None
            assert campaign.status in {CampaignStatus.COMPLETED, CampaignStatus.PROCESSING}

    def test_one_campaign_is_left_as_a_draft_to_launch_by_hand(self, seeded) -> None:
        """So there is something to run through the real pipeline."""
        assert Campaign.objects.filter(status=CampaignStatus.DRAFT).count() == 1

    def test_the_template_it_creates_is_local_and_unsubmitted(self, seeded) -> None:
        """Nothing here may mark a template approved — that is Meta's to decide."""
        template = MessageTemplate.objects.get()

        assert template.source == "local"
        assert template.status == "not_submitted"

    def test_the_history_is_spread_over_the_requested_window(self, superuser) -> None:
        from datetime import timedelta

        from django.utils import timezone

        call_command("seed_demo", contacts=20, campaigns=4, days=30, verbosity=0)

        started = list(
            Campaign.objects.exclude(started_at=None).values_list("started_at", flat=True)
        )
        assert len(set(started)) > 1, "every campaign launched at the same moment"
        assert min(started) >= timezone.now() - timedelta(days=31)

    def test_it_is_deterministic_for_a_given_seed(self, superuser) -> None:
        call_command("seed_demo", contacts=15, campaigns=2, seed=99, verbosity=0)
        first = sorted(Contact.objects.values_list("name", flat=True))

        call_command("seed_demo", clear=True, verbosity=0)
        call_command("seed_demo", contacts=15, campaigns=2, seed=99, verbosity=0)

        assert sorted(Contact.objects.values_list("name", flat=True)) == first


class TestClear:
    def test_it_removes_what_it_created(self, seeded) -> None:
        call_command("seed_demo", clear=True, verbosity=0)

        assert Contact.objects.count() == 0
        assert Campaign.objects.count() == 0
        assert ContactGroup.objects.count() == 0
        assert MessageTemplate.objects.count() == 0
        assert Message.objects.count() == 0

    def test_it_leaves_real_data_alone(self, seeded, make_contact, organization) -> None:
        """
        The marker is what separates seeded rows from a person's own work.
        Clearing must never be a way to lose real contacts.
        """
        real = make_contact("A real person", "+9779812345678", opted_in=True)
        real_group = ContactGroup.objects.create(name="A real group", organization=organization)

        call_command("seed_demo", clear=True, verbosity=0)

        assert Contact.objects.filter(pk=real.pk).exists()
        assert ContactGroup.objects.filter(pk=real_group.pk).exists()

    def test_clearing_an_unseeded_database_is_harmless(self, superuser) -> None:
        call_command("seed_demo", clear=True, verbosity=0)
        assert Contact.objects.count() == 0

    def test_clearing_is_refused_against_a_live_provider_too(
        self, seeded, settings
    ) -> None:
        """The guard is on the command, not on one branch of it."""
        settings.WHATSAPP_PROVIDER = "meta"

        with pytest.raises(CommandError):
            call_command("seed_demo", clear=True, verbosity=0)

        assert Contact.objects.count() == 12

    def test_seeding_twice_does_not_duplicate_the_groups(self, seeded, superuser) -> None:
        call_command("seed_demo", contacts=5, campaigns=2, verbosity=0)

        assert ContactGroup.objects.count() == 3


class TestRunningItTwice:
    """
    Running the command a second time is an ordinary thing to do, and every
    generated phone number collides on that second run. Before this was
    handled the command ended up with no audience at all and crashed trying to
    pick group members from an empty list.
    """

    def test_a_second_run_still_produces_a_working_audience(self, superuser) -> None:
        call_command("seed_demo", contacts=10, campaigns=2, verbosity=0)
        call_command("seed_demo", contacts=10, campaigns=2, verbosity=0)

        assert Contact.objects.count() == 10
        assert Message.objects.exists()

    def test_a_second_run_reuses_the_contacts_rather_than_orphaning_them(
        self, superuser
    ) -> None:
        call_command("seed_demo", contacts=10, campaigns=2, verbosity=0)
        Campaign.objects.all().delete()

        call_command("seed_demo", contacts=10, campaigns=2, verbosity=0)

        for campaign in Campaign.objects.exclude(status=CampaignStatus.DRAFT):
            assert campaign.messages.exists(), campaign.name

    def test_a_contact_someone_entered_by_hand_is_never_swept_in(
        self, superuser, make_contact
    ) -> None:
        """
        A number a person entered themselves is not demo data. If the seeder
        adopted it, --clear would later delete a real contact.
        """
        real = make_contact("A real person", "+9779810000000", opted_in=True)
        original_notes = real.notes

        call_command("seed_demo", contacts=10, campaigns=2, verbosity=0)

        real.refresh_from_db()
        assert real.notes == original_notes
        assert real.group_memberships.count() == 0

        call_command("seed_demo", clear=True, verbosity=0)
        assert Contact.objects.filter(pk=real.pk).exists()
