"""
Verify the live Meta integration, end to end, in one command.

Everything in the test suite runs against stubbed HTTP. That proves the code
does what Meta's documentation says; it cannot prove Meta agrees. This is the
step that closes that gap, and it exists as a command rather than as a
checklist in the README because a checklist gets half-followed once and never
again.

    python manage.py verify_live                        # read-only checks
    python manage.py verify_live --to +9779800000000    # ...and a real send

Without ``--to`` nothing leaves the machine: it checks configuration, the
sending pipeline, and what Meta says your templates are. With ``--to`` it
launches a genuine one-recipient campaign and waits for the delivery webhooks
to come back.

**The send goes through the ordinary campaign path**, not around it — the same
``launch_campaign`` an operator's click uses, so what it verifies is the thing
that actually runs in production rather than a special case that only exists
here. The recipient is resolved through ``Contact.objects.eligible()`` with no
override, which means the number you verify with has to be a contact who has
consented. That is not an obstacle to work around; it is the rule the whole
system is built on, and a verification command that could message someone
without consent would be a hole in it.
"""

from __future__ import annotations

import time
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

OK = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"


class Command(BaseCommand):
    help = "Check the live Meta integration, optionally sending one real message."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--to",
            default="",
            help="Send one real message to this E.164 number. It must belong to a contact who has consented.",
        )
        parser.add_argument(
            "--template",
            default="",
            help="Template name to send. Defaults to the first approved one.",
        )
        parser.add_argument(
            "--wait",
            type=int,
            default=60,
            help="Seconds to wait for delivery webhooks after sending (default 60).",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt before sending a real message.",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Write the fetched templates into the local mirror, rather than only reporting them.",
        )

    # -- Reporting ----------------------------------------------------------

    def report(self, outcome: str, title: str, detail: str = "") -> str:
        colour = {
            OK: self.style.SUCCESS,
            FAIL: self.style.ERROR,
            WARN: self.style.WARNING,
            SKIP: self.style.HTTP_INFO,
        }[outcome]
        self.stdout.write(f"  {colour(outcome.ljust(4))}  {title}")
        if detail:
            self.stdout.write(f"        {detail}")
        return outcome

    # -- Entry point --------------------------------------------------------

    def handle(self, *args, **options) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Live integration check"))
        self.stdout.write("")

        outcomes = [
            self.check_provider(),
            self.check_configuration(),
            self.check_pipeline(),
            self.check_templates(sync=options["sync"]),
        ]

        if options["to"]:
            outcomes.append(
                self.check_send(
                    to=options["to"],
                    template_name=options["template"],
                    wait=options["wait"],
                    assume_yes=options["yes"],
                )
            )
        else:
            outcomes.append(
                self.report(
                    SKIP,
                    "Send a real message",
                    "Pass --to +<number> to send. Nothing has left this machine.",
                )
            )

        self.stdout.write("")
        failed = outcomes.count(FAIL)
        if failed:
            self.stdout.write(self.style.ERROR(f"{failed} check(s) failed."))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("No failures."))
        self.stdout.write("")

    # -- Checks -------------------------------------------------------------

    def check_provider(self) -> str:
        """A live check against the mock proves nothing at all."""
        from whatsapp.services.factory import provider_name

        name = provider_name()
        if name != "meta":
            raise CommandError(
                f"WHATSAPP_PROVIDER is '{name}'. This command verifies the live "
                "integration; against the mock it would only be testing itself. "
                "Set WHATSAPP_PROVIDER=meta with real credentials."
            )
        return self.report(OK, "Provider is 'meta'")

    def check_configuration(self) -> str:
        """Report *which* settings are missing, and never what they contain."""
        from django.conf import settings

        from core.exceptions import ProviderNotConfigured
        from whatsapp.services.factory import get_provider

        try:
            get_provider().check_configuration()
        except ProviderNotConfigured as exc:
            return self.report(FAIL, "Credentials", exc.message)

        optional = {
            "META_WABA_ID": "template sync",
            "META_APP_SECRET": "webhook signature checks",
            "META_WEBHOOK_VERIFY_TOKEN": "the webhook subscription handshake",
        }
        missing = [
            f"{name} (needed for {purpose})"
            for name, purpose in optional.items()
            if not getattr(settings, name, "")
        ]
        if missing:
            return self.report(WARN, "Credentials", "Set, but incomplete: " + "; ".join(missing))

        return self.report(OK, "Credentials", "All META_* settings are present.")

    def _organization(self):
        """
        Whose templates to sync into.

        A command line has no session, so this takes the first active
        organization — unambiguous while a deployment has one, and the single
        place to add a --organization flag when that stops being true.
        """
        from organizations.models import Organization

        organization = Organization.objects.active().order_by("created_at").first()
        if organization is None:
            raise CommandError(
                "No active organization exists to sync templates into. "
                "Create one before running this."
            )
        return organization

    def check_pipeline(self) -> str:
        """A registered sender is not a reachable queue; this asks about both."""
        from whatsapp.health import pipeline_status

        status = pipeline_status()
        detail = (
            f"dispatcher={'yes' if status['dispatcher_registered'] else 'no'}, "
            f"broker={'reachable' if status['broker_reachable'] else 'unreachable'}, "
            f"ceiling={status['send_rate_per_second']}/s"
        )
        if not status["can_send"]:
            return self.report(
                FAIL, "Sending pipeline", f"{detail}. {status.get('broker_detail', '')}".strip()
            )
        return self.report(OK, "Sending pipeline", detail)

    def check_templates(self, *, sync: bool) -> str:
        """
        Ask Meta what it thinks your templates are.

        This is the first call that actually leaves the machine, so it is also
        the real test of whether the access token works.
        """
        from django.conf import settings

        from core.exceptions import DomainError
        from whatsapp.models import MessageTemplate
        from whatsapp.services.factory import get_provider
        from whatsapp.services.templates import sync_templates_from_provider

        if not getattr(settings, "META_WABA_ID", ""):
            # The configuration check already reported this. Failing here as
            # well would report one missing setting twice, and escalate a
            # warning into a failure on the way.
            return self.report(
                SKIP, "Templates", "Needs META_WABA_ID, which is reported above."
            )

        try:
            if sync:
                count = sync_templates_from_provider(organization=self._organization())
                approved = MessageTemplate.objects.filter(status="approved").count()
                return self.report(
                    OK, "Templates", f"Synced {count} from Meta; {approved} approved."
                )

            fetched = get_provider().fetch_templates()
        except DomainError as exc:
            return self.report(FAIL, "Templates", exc.message)
        except Exception as exc:  # noqa: BLE001 - report, never traceback at the operator
            return self.report(FAIL, "Templates", f"{exc.__class__.__name__}: {exc}")

        approved = [t for t in fetched if t.status == "approved"]
        if not fetched:
            return self.report(
                WARN, "Templates", "Meta reports no templates on this WABA."
            )
        if not approved:
            return self.report(
                WARN,
                "Templates",
                f"{len(fetched)} template(s), none approved. Business-initiated messages need an approved one.",
            )
        return self.report(
            OK,
            "Templates",
            f"{len(fetched)} on the WABA, {len(approved)} approved. "
            "Re-run with --sync to mirror them locally.",
        )

    # -- The real send ------------------------------------------------------

    def check_send(self, *, to: str, template_name: str, wait: int, assume_yes: bool) -> str:
        from campaigns.models import CampaignMessageType
        from core.exceptions import DomainError

        contact = self._resolve_recipient(to)
        template = self._resolve_template(template_name)

        if not assume_yes:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"About to send a real WhatsApp message to {contact.name} "
                    f"<{contact.phone_number}> using template '{template.name}'."
                )
            )
            if input("  Type 'send' to continue: ").strip().lower() != "send":
                return self.report(SKIP, "Send a real message", "Cancelled.")

        try:
            campaign = self._launch(contact, template, CampaignMessageType.TEMPLATE)
        except DomainError as exc:
            return self.report(FAIL, "Send a real message", exc.message)

        message = campaign.messages.first()
        if message is None:
            return self.report(FAIL, "Send a real message", "No message row was created.")

        self.stdout.write("")
        return self._await_delivery(message, wait)

    def _resolve_recipient(self, to: str):
        """
        The recipient must be a contact who has consented. No override.

        Adding your own number as a consenting contact is a true statement and
        an audited one. A flag that skipped this would be a way to message
        someone without consent, which is the one thing this system does not
        have.
        """
        from contacts.models import Contact
        from core.phone import PhoneNumberError, normalize_phone_number

        try:
            number = normalize_phone_number(to)
        except PhoneNumberError as exc:
            raise CommandError(f"{to} is not a usable phone number: {exc}") from exc

        contact = Contact.objects.eligible().filter(phone_number=number).first()
        if contact is None:
            raise CommandError(
                f"{number} is not an opted-in, active contact, so it cannot be messaged.\n"
                "Add it as a contact and record consent first — through the UI, or:\n"
                "  python manage.py shell -c \"from contacts.services import create_contact; "
                f"create_contact(name='Verification', phone_number='{number}', "
                "opted_in=True, opt_in_source='manual')\""
            )
        return contact

    def _resolve_template(self, name: str):
        from whatsapp.models import MessageTemplate

        queryset = MessageTemplate.objects.filter(status="approved", source="synced")
        if name:
            template = queryset.filter(name=name).first()
            if template is None:
                raise CommandError(
                    f"No approved, synced template named '{name}'. "
                    "Run with --sync first, or check the name in WhatsApp Manager."
                )
            return template

        template = queryset.order_by("name").first()
        if template is None:
            raise CommandError(
                "No approved template is mirrored locally. Run "
                "`python manage.py verify_live --sync` first."
            )
        return template

    def _launch(self, contact, template, message_type):
        """Build and launch a one-recipient campaign through the ordinary path."""
        from campaigns.services import (
            create_campaign,
            launch_campaign,
            set_audience,
            set_message,
        )
        from contacts.models import ContactGroup, GroupMembership

        group, _ = ContactGroup.objects.get_or_create(
            name="Live verification",
            defaults={"description": "Recipients of `manage.py verify_live` checks."},
        )
        GroupMembership.objects.get_or_create(group=group, contact=contact)

        stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        campaign = create_campaign(name=f"Live verification {stamp}", description="verify_live")
        set_audience(campaign, [group])
        set_message(
            campaign,
            message_type=message_type,
            template=template,
            variable_mapping={
                placeholder: (
                    {"source": "contact_field", "value": "name"}
                    if placeholder == "name"
                    else {"source": "literal", "value": "verification"}
                )
                for placeholder in (template.variables or [])
            },
        )
        return launch_campaign(campaign)

    def _await_delivery(self, message, wait: int) -> str:
        """
        Watch one message for as long as the operator allowed.

        Reaching "sent" proves the send path. Reaching "delivered" proves the
        webhook path too — which is the half that cannot be checked any other
        way, because it depends on Meta being able to reach *us*.
        """
        from messaging.models import MessageStatus

        deadline = timezone.now() + timedelta(seconds=wait)
        seen: list[str] = []

        self.stdout.write(f"  Watching for up to {wait}s...")
        while timezone.now() < deadline:
            message.refresh_from_db()
            if message.status not in seen:
                seen.append(message.status)
                self.stdout.write(f"        {message.get_status_display().lower()}")
            if message.status in {MessageStatus.DELIVERED, MessageStatus.READ}:
                return self.report(
                    OK,
                    "Send a real message",
                    f"Delivered, confirmed by webhook. Path: {' -> '.join(seen)}",
                )
            if message.status == MessageStatus.FAILED:
                return self.report(
                    FAIL,
                    "Send a real message",
                    f"Meta reported {message.error_code}: {message.error_message}",
                )
            time.sleep(2)

        message.refresh_from_db()
        if message.status == MessageStatus.SENT:
            return self.report(
                WARN,
                "Send a real message",
                "Meta accepted the message but no delivery webhook arrived in time. "
                "The send path works; check the callback URL, the verify token, and that "
                "the 'messages' field is subscribed.",
            )
        return self.report(
            WARN,
            "Send a real message",
            f"Still {message.get_status_display().lower()} after {wait}s. "
            "Is a Celery worker running on the whatsapp_send queue?",
        )
