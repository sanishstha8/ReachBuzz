"""
Populate a development database with realistic campaign history.

The dashboard and the reports page are hard to judge against an empty
database: an honest empty state is exactly what they are designed to show, so
there is nothing to look at. This fills in a plausible few weeks of sending.

**It goes through the real services.** Contacts are created with
``contacts.services.create_contact`` and consented with ``set_consent`` so the
audit trail is written; campaigns move through ``campaigns.services.transition``
so the state machine is respected; recipients come from
``materialize_messages``. Seed data that took shortcuts around those would not
be representative of the thing it is meant to demonstrate — and would quietly
create the second, unaudited consent path the project forbids.

The one thing that is not a real code path is the backdating at the end: you
cannot retroactively send a campaign, so timestamps are rewritten afterwards to
spread the history over the requested window. That is stated here rather than
hidden, and it is why this command is refused outside development.

    python manage.py seed_demo
    python manage.py seed_demo --contacts 300 --campaigns 10 --days 60
    python manage.py seed_demo --clear
"""

from __future__ import annotations

import random
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

# Written into the notes/description of everything this command creates, so
# --clear can remove exactly what it made and nothing a person typed by hand.
MARKER = "[seed_demo]"

FIRST_NAMES = [
    "Aarav", "Anisha", "Bibek", "Sita", "Prakash", "Nisha", "Rajesh", "Sunita",
    "Kiran", "Manisha", "Dipesh", "Puja", "Suman", "Rekha", "Niraj", "Sarita",
]
LAST_NAMES = [
    "Shrestha", "Gurung", "Tamang", "Adhikari", "Karki", "Rai", "Thapa",
    "Magar", "Bhattarai", "Poudel", "Sherpa", "Limbu",
]

GROUPS = [
    ("Newsletter", "Everyone who asked for monthly updates."),
    ("Loyalty members", "Customers enrolled in the loyalty programme."),
    ("Recent buyers", "Ordered in the last ninety days."),
]

CAMPAIGNS = [
    "Dashain Offer", "New Year Greetings", "Order Ready Wave 3",
    "Loyalty Reminder", "Stock Back In", "Weekend Flash Sale",
    "Winter Clearance", "Free Delivery Week", "Membership Renewal",
    "Festival Preview",
]

# Roughly what a healthy send looks like: most delivered, many read, a few
# still in flight, a small tail of failures.
FAILURES = [
    ("131026", "Message undeliverable"),
    ("470", "Outside the service window"),
    ("131047", "Re-engagement message required"),
]


class Command(BaseCommand):
    help = "Fill a development database with demonstration contacts, campaigns and messages."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--contacts", type=int, default=120)
        parser.add_argument("--campaigns", type=int, default=6)
        parser.add_argument("--days", type=int, default=45, help="Span of history to create.")
        parser.add_argument("--seed", type=int, default=20260902, help="Random seed.")
        parser.add_argument(
            "--clear", action="store_true", help="Delete previously seeded data and stop."
        )

    def handle(self, *args, **options) -> None:
        self._refuse_outside_development()

        if options["clear"]:
            self._clear()
            return

        random.seed(options["seed"])
        user = self._demo_user()

        contacts = self._create_contacts(options["contacts"], user)
        groups = self._create_groups(contacts, user)
        template = self._create_template(user)
        self._create_campaigns(
            count=options["campaigns"],
            days=options["days"],
            groups=groups,
            template=template,
            user=user,
        )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo data created."))
        self.stdout.write("  Dashboard  http://127.0.0.1:8000/")
        self.stdout.write("  Reports    http://127.0.0.1:8000/reports/")
        self.stdout.write("")
        self.stdout.write("Remove it again with: python manage.py seed_demo --clear")

    # -- Guards -------------------------------------------------------------

    @staticmethod
    def _refuse_outside_development() -> None:
        """
        Never let fabricated contacts near a system wired to a real provider.

        Two independent checks rather than one: DEBUG can be left on by
        accident, and a provider setting can be switched without restarting a
        shell. Either alone being wrong should not be enough.
        """
        if getattr(settings, "WHATSAPP_PROVIDER", "mock") != "mock":
            raise CommandError(
                "seed_demo refuses to run unless WHATSAPP_PROVIDER=mock. "
                "These are fabricated people, and this system sends real messages."
            )
        if not settings.DEBUG:
            raise CommandError("seed_demo refuses to run with DEBUG=False.")

    def _demo_user(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.order_by("-is_superuser", "date_joined").first()
        if user is None:
            raise CommandError(
                "No user exists to own the demo data. Run: python manage.py createsuperuser"
            )
        return user

    # -- Clearing -----------------------------------------------------------

    def _clear(self) -> None:
        """
        Remove seeded rows, in an order the protective foreign keys permit.

        Campaigns first: ``CampaignAudience.group`` is PROTECT, so a group
        cannot be deleted while a campaign still points at it.
        """
        from campaigns.models import Campaign
        from contacts.models import Contact, ContactGroup
        from whatsapp.models import MessageTemplate

        with transaction.atomic():
            campaigns, _ = Campaign.objects.filter(description__contains=MARKER).delete()
            groups, _ = ContactGroup.objects.filter(description__contains=MARKER).delete()
            contacts, _ = Contact.objects.filter(notes__contains=MARKER).delete()
            templates, _ = MessageTemplate.objects.filter(
                example_values__seed_demo=True
            ).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Removed {campaigns} campaign rows, {groups} group rows, "
                f"{contacts} contact rows and {templates} template rows."
            )
        )

    # -- Creation -----------------------------------------------------------

    def _create_contacts(self, count: int, user) -> list:
        from contacts.models import Contact, ContactStatus, OptInSource
        from contacts.services import create_contact, set_consent
        from core.exceptions import ConflictError

        created = []
        for index in range(count):
            name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
            # Consent and status are rolled independently, and that matters:
            # derived from one roll, every inactive contact would also be
            # un-consented, "eligible" would always equal "opted in", and the
            # demo would never show the case the consent rule exists for — a
            # contact who consented but still may not be messaged.
            consenting = random.random() < 0.82
            status_roll = random.random()
            if status_roll < 0.92:
                status = ContactStatus.ACTIVE
            elif status_roll < 0.97:
                status = ContactStatus.INACTIVE
            else:
                status = ContactStatus.BLOCKED

            phone_number = f"+97798{index + 10_000_000:08d}"
            try:
                contact = create_contact(
                    name=name,
                    phone_number=phone_number,
                    email=f"demo{index}@example.invalid",
                    status=status,
                    notes=MARKER,
                    user=user,
                )
            except ConflictError:
                # Running the command twice is an ordinary thing to do, and
                # every number collides on the second run. Reuse the row we
                # made last time rather than ending up with no audience at
                # all — but only if it is ours: a number a person entered
                # themselves is not demo data to be swept into demo groups.
                contact = Contact.objects.filter(
                    phone_number=phone_number, notes__contains=MARKER
                ).first()
                if contact is None:
                    continue
                created.append(contact)
                continue

            if consenting:
                # Through the service, so the opt-in is audited like any other.
                set_consent(
                    contact,
                    opted_in=True,
                    source=random.choice(
                        [OptInSource.WEB_FORM, OptInSource.CSV_IMPORT, OptInSource.MANUAL]
                    ),
                    user=user,
                )
            created.append(contact)

        self.stdout.write(f"Contacts      {len(created)}")
        return created

    def _create_groups(self, contacts: list, user) -> list:
        from contacts.models import ContactGroup
        from contacts.services import add_contacts_to_group_bulk

        groups = []
        for name, description in GROUPS:
            group, _ = ContactGroup.objects.get_or_create(
                name=name, defaults={"description": f"{description} {MARKER}"}
            )
            groups.append(group)
            if not contacts:
                continue
            share = int(len(contacts) * random.uniform(0.3, 0.7))
            members = random.sample(contacts, k=max(1, min(share, len(contacts))))
            add_contacts_to_group_bulk([group], members, user=user)

        self.stdout.write(f"Groups        {len(groups)}")
        return groups

    def _create_template(self, user):
        """
        A local, unsubmitted template.

        Nothing here marks a template approved: approval belongs to Meta, and
        a local template is refused under the live provider anyway.
        """
        from whatsapp.models import MessageTemplate, TemplateCategory, TemplateSource, TemplateStatus

        template, _ = MessageTemplate.objects.get_or_create(
            name="seed_demo_offer",
            defaults={
                "language": "en",
                "category": TemplateCategory.MARKETING,
                "source": TemplateSource.LOCAL,
                "status": TemplateStatus.NOT_SUBMITTED,
                "body_text": "Hello {{name}}, our {{offer}} is on now. Reply STOP to opt out.",
                "example_values": {"seed_demo": True},
                "created_by": user,
            },
        )
        return template

    def _create_campaigns(self, *, count: int, days: int, groups: list, template, user) -> None:
        from campaigns.models import CampaignMessageType, CampaignStatus
        from campaigns.services import (
            create_campaign,
            materialize_messages,
            resolve_audience,
            set_audience,
            set_message,
            transition,
        )

        names = CAMPAIGNS[:count] if count <= len(CAMPAIGNS) else CAMPAIGNS
        now = timezone.now()
        sent = 0

        for index, name in enumerate(names):
            campaign = create_campaign(
                name=name, description=f"Demonstration campaign. {MARKER}", user=user
            )
            set_audience(campaign, random.sample(groups, k=random.randint(1, len(groups))))
            set_message(
                campaign,
                message_type=CampaignMessageType.TEMPLATE,
                template=template,
                variable_mapping={
                    "name": {"source": "contact_field", "value": "name"},
                    "offer": {"source": "literal", "value": f"{random.choice([10, 15, 20, 25])}% off"},
                },
            )

            # The last one stays a draft, so there is something to launch by
            # hand and watch move through the real pipeline.
            if index == len(names) - 1:
                self.stdout.write(f"Campaigns     {sent} with history, 1 draft ready to launch")
                return

            recipients = list(resolve_audience(campaign))
            if not recipients:
                continue

            transition(campaign, CampaignStatus.PROCESSING)
            materialize_messages(campaign, recipients)

            started = now - timedelta(days=days * (index + 1) // (len(names) + 1), hours=index)
            still_sending = index == 0
            self._age_campaign(campaign, started=started, still_sending=still_sending)
            sent += 1

        self.stdout.write(f"Campaigns     {sent}")

    def _age_campaign(self, campaign, *, started, still_sending: bool) -> None:
        """
        Give a campaign a past.

        This is the one place that writes state directly rather than through a
        service, because there is no honest way to ask the system to have sent
        something last Tuesday. Everything it writes is what a real send would
        have left behind: a status, its matching timestamp, and — for failures
        — the error the provider would have reported.
        """
        from campaigns.models import CampaignStatus
        from campaigns.services import transition
        from messaging.models import Message, MessageStatus

        updates = []
        for message in Message.objects.filter(campaign=campaign):
            roll = random.random()
            created = started + timedelta(minutes=random.randint(0, 90))

            if still_sending and roll < 0.30:
                message.status = MessageStatus.QUEUED
                message.queued_at = created
            elif roll < 0.42:
                message.status = MessageStatus.READ
                message.sent_at = created
                message.delivered_at = created + timedelta(seconds=random.randint(5, 60))
                message.read_at = message.delivered_at + timedelta(minutes=random.randint(1, 300))
            elif roll < 0.88:
                message.status = MessageStatus.DELIVERED
                message.sent_at = created
                message.delivered_at = created + timedelta(seconds=random.randint(5, 90))
            elif roll < 0.94:
                message.status = MessageStatus.SENT
                message.sent_at = created
            else:
                code, detail = random.choice(FAILURES)
                message.status = MessageStatus.FAILED
                message.error_code = code
                message.error_message = detail
                message.failed_at = created + timedelta(seconds=random.randint(1, 30))

            message.created_at = created
            updates.append(message)

        Message.objects.bulk_update(
            updates,
            ["status", "queued_at", "sent_at", "delivered_at", "read_at", "failed_at",
             "error_code", "error_message", "created_at"],
            batch_size=500,
        )

        campaign.started_at = started
        campaign.total_recipients = len(updates)
        if still_sending:
            campaign.save(update_fields=["started_at", "total_recipients", "updated_at"])
            return

        transition(campaign, CampaignStatus.COMPLETED, save=False)
        campaign.completed_at = started + timedelta(hours=random.randint(1, 4))
        campaign.save(
            update_fields=["status", "started_at", "completed_at", "total_recipients", "updated_at"]
        )
