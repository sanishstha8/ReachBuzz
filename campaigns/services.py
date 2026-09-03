"""
Campaign business logic: audience resolution, validation, lifecycle.

The rule that matters most lives in :func:`resolve_audience`. It filters on
``Contact.objects.eligible()`` and takes no override argument, so there is no
code path — API, HTML, admin or otherwise — that can message someone whose
consent is not on record.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from campaigns import dispatch, variables
from campaigns.models import (
    ALLOWED_TRANSITIONS,
    Campaign,
    CampaignAudience,
    CampaignMessageType,
    CampaignStatus,
)
from contacts.models import Contact, ContactGroup, ContactStatus
from core.audit import record_audit
from core.exceptions import InvalidStateTransition, ValidationFailed
from core.models import AuditAction
from whatsapp.services.templates import render_template

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audience
# ---------------------------------------------------------------------------


@dataclass
class AudienceBreakdown:
    """Who is in the audience, and who was left out and why."""

    in_audience: int = 0
    eligible: int = 0
    excluded_not_opted_in: int = 0
    excluded_inactive: int = 0
    groups: list[str] = field(default_factory=list)
    targets_all: bool = False

    @property
    def excluded_total(self) -> int:
        return self.excluded_not_opted_in + self.excluded_inactive


def audience_queryset(campaign: Campaign) -> QuerySet[Contact]:
    """Every contact the campaign targets, before consent is applied."""
    if campaign.target_all_eligible:
        return Contact.objects.all()

    group_ids = list(campaign.audience_entries.values_list("group_id", flat=True))
    if not group_ids:
        return Contact.objects.none()

    return Contact.objects.filter(group_memberships__group_id__in=group_ids).distinct()


def resolve_audience(campaign: Campaign) -> QuerySet[Contact]:
    """
    The recipients this campaign may actually message.

    Consent is applied here and nowhere else, and there is deliberately no
    parameter to skip it.
    """
    return audience_queryset(campaign).eligible().distinct()


def audience_breakdown(campaign: Campaign) -> AudienceBreakdown:
    """Counts for the preview screen, in one query."""
    queryset = audience_queryset(campaign)

    counts = queryset.aggregate(
        total=Count("id", distinct=True),
        eligible=Count(
            "id",
            filter=Q(opted_in=True, status=ContactStatus.ACTIVE),
            distinct=True,
        ),
        not_opted_in=Count("id", filter=Q(opted_in=False), distinct=True),
        inactive=Count(
            "id",
            filter=Q(opted_in=True) & ~Q(status=ContactStatus.ACTIVE),
            distinct=True,
        ),
    )

    return AudienceBreakdown(
        in_audience=counts["total"],
        eligible=counts["eligible"],
        excluded_not_opted_in=counts["not_opted_in"],
        excluded_inactive=counts["inactive"],
        groups=list(campaign.audience_groups.values_list("name", flat=True)),
        targets_all=campaign.target_all_eligible,
    )


@transaction.atomic
def set_audience(
    campaign: Campaign,
    groups: Iterable[ContactGroup],
    *,
    target_all_eligible: bool = False,
) -> Campaign:
    """Replace the campaign's audience. Only permitted while it is editable."""
    _require_editable(campaign, "The audience can only be changed while a campaign is a draft.")

    campaign.target_all_eligible = target_all_eligible
    campaign.save(update_fields=["target_all_eligible", "updated_at"])

    campaign.audience_entries.all().delete()
    if not target_all_eligible:
        CampaignAudience.objects.bulk_create(
            [CampaignAudience(campaign=campaign, group=group) for group in groups],
            ignore_conflicts=True,
        )

    return campaign


# ---------------------------------------------------------------------------
# Message content
# ---------------------------------------------------------------------------


@dataclass
class CampaignPreview:
    """Everything the confirmation screen needs before a send."""

    campaign: Campaign
    audience: AudienceBreakdown
    sample_recipient: Contact | None = None
    sample_text: str = ""
    missing_variables: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        return not self.blockers


def render_for_contact(campaign: Campaign, contact: Contact) -> dict:
    """Resolve the campaign's message for one recipient."""
    if campaign.message_type == CampaignMessageType.TEXT:
        return {
            "type": CampaignMessageType.TEXT,
            "text": campaign.body_text,
            "values": {},
            "missing": [],
        }

    values = variables.resolve_values(campaign.variable_mapping, contact)
    rendered = render_template(campaign.template, values)

    return {
        "type": CampaignMessageType.TEMPLATE,
        "template_name": campaign.template.name,
        "language": campaign.template.language,
        "text": rendered.full_text,
        "values": values,
        "missing": rendered.missing,
    }


def validation_blockers(campaign: Campaign) -> list[str]:
    """
    Human-readable reasons this campaign cannot be sent.

    Returned as a list rather than raised so the wizard can show every problem
    at once instead of one per submission.
    """
    blockers: list[str] = []
    provider = getattr(settings, "WHATSAPP_PROVIDER", "mock")

    if not campaign.name.strip():
        blockers.append("The campaign needs a name.")

    if not campaign.target_all_eligible and not campaign.audience_entries.exists():
        blockers.append("Select at least one group, or target all eligible contacts.")

    if campaign.message_type == CampaignMessageType.TEMPLATE:
        if campaign.template is None:
            blockers.append("Select a message template.")
        else:
            usability = campaign.template.usability(provider)
            if not usability.usable:
                blockers.append(usability.reason)

            unmapped = campaign.unmapped_variables
            if unmapped:
                blockers.append(
                    "Provide a value for every template variable: "
                    + ", ".join(f"{{{{{token}}}}}" for token in unmapped)
                )
    elif not campaign.body_text.strip():
        blockers.append("Enter the message text.")

    eligible = resolve_audience(campaign).count()
    if eligible == 0:
        blockers.append(
            "No recipient in this audience has recorded consent, so there is nobody to message."
        )

    maximum = getattr(settings, "CAMPAIGN_MAX_RECIPIENTS", 5000)
    if eligible > maximum:
        blockers.append(
            f"This audience has {eligible:,} recipients, above the configured limit of "
            f"{maximum:,}. Split it into smaller campaigns."
        )

    return blockers


def preview_campaign(campaign: Campaign) -> CampaignPreview:
    """Build the confirmation-screen preview."""
    breakdown = audience_breakdown(campaign)
    blockers = validation_blockers(campaign)

    sample_recipient = resolve_audience(campaign).order_by("name").first()
    sample_text = ""
    missing: list[str] = []

    if campaign.message_type == CampaignMessageType.TEXT:
        sample_text = campaign.body_text
    elif campaign.template is not None:
        if sample_recipient is not None:
            rendered = render_for_contact(campaign, sample_recipient)
            sample_text = rendered["text"]
            missing = rendered["missing"]
        else:
            # No eligible recipient yet: show the shape of the message using
            # representative values, clearly derived from the mapping.
            values = variables.sample_contact_values(campaign.variable_mapping)
            rendered = render_template(campaign.template, values)
            sample_text = rendered.full_text
            missing = rendered.missing

    return CampaignPreview(
        campaign=campaign,
        audience=breakdown,
        sample_recipient=sample_recipient,
        sample_text=sample_text,
        missing_variables=missing,
        blockers=blockers,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _require_editable(campaign: Campaign, message: str) -> None:
    if not campaign.is_editable:
        raise InvalidStateTransition(
            f"{message} This campaign is {campaign.get_status_display().lower()}."
        )


def transition(campaign: Campaign, to_status: str, *, save: bool = True) -> Campaign:
    """
    Move the campaign to ``to_status`` if the state machine permits it.

    Every lifecycle change goes through here, so an illegal move (relaunching a
    completed campaign, say) fails loudly rather than silently double-sending.
    """
    allowed = ALLOWED_TRANSITIONS.get(campaign.status, set())
    if to_status not in allowed:
        raise InvalidStateTransition(
            f"A {campaign.get_status_display().lower()} campaign cannot become "
            f"{CampaignStatus(to_status).label.lower()}."
        )

    campaign.status = to_status
    if save:
        campaign.save(update_fields=["status", "updated_at"])
    return campaign


@transaction.atomic
def create_campaign(
    *,
    name: str,
    description: str = "",
    organization=None,
    user=None,
    request: HttpRequest | None = None,
) -> Campaign:
    """Create a draft campaign. The wizard fills in the rest step by step."""
    name = (name or "").strip()
    if not name:
        raise ValidationFailed("A campaign name is required.", details={"name": ["Enter a name."]})

    campaign = Campaign.objects.create(
        name=name, description=description or "", organization=organization, created_by=user
    )

    record_audit(
        AuditAction.CAMPAIGN_CREATED,
        user=user,
        request=request,
        obj=campaign,
        description=f"Created campaign {campaign.name}",
    )
    return campaign


@transaction.atomic
def set_message(
    campaign: Campaign,
    *,
    message_type: str,
    template=None,
    body_text: str = "",
    variable_mapping: dict | None = None,
) -> Campaign:
    """Attach the message content, validating the variable mapping."""
    _require_editable(campaign, "The message can only be changed while a campaign is a draft.")

    campaign.message_type = message_type

    if message_type == CampaignMessageType.TEMPLATE:
        if template is None:
            raise ValidationFailed(
                "Select a template.", details={"template": ["A template is required."]}
            )
        mapping = variable_mapping or {}
        variables.validate_mapping(mapping, list(template.variables or []))

        campaign.template = template
        campaign.variable_mapping = mapping
        campaign.body_text = ""
    else:
        if not (body_text or "").strip():
            raise ValidationFailed(
                "Enter the message text.", details={"body_text": ["The message cannot be empty."]}
            )
        campaign.template = None
        campaign.variable_mapping = {}
        campaign.body_text = body_text

    campaign.save(
        update_fields=[
            "message_type",
            "template",
            "variable_mapping",
            "body_text",
            "updated_at",
        ]
    )
    return campaign


def materialize_messages(campaign: Campaign, contacts: Iterable[Contact]) -> int:
    """
    Create one Message row per recipient.

    ``ignore_conflicts`` plus ``unique(campaign, contact)`` makes this safe to
    run twice: a retried launch tops up the missing rows instead of duplicating
    the whole audience.
    """
    from messaging.models import Message, MessageStatus

    rows = []
    for contact in contacts:
        rendered = render_for_contact(campaign, contact)
        rows.append(
            Message(
                campaign=campaign,
                # Derived, never passed: a message cannot belong to a different
                # organization than the campaign that created it.
                organization_id=campaign.organization_id,
                contact=contact,
                to_phone_number=contact.phone_number,
                message_type=campaign.message_type,
                template=campaign.template,
                template_name=campaign.template.name if campaign.template else "",
                template_language=campaign.template.language if campaign.template else "",
                rendered_payload=rendered,
                status=MessageStatus.PENDING,
            )
        )

    if not rows:
        return 0

    Message.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)
    return Message.objects.filter(campaign=campaign).count()


@transaction.atomic
def launch_campaign(
    campaign: Campaign,
    *,
    user=None,
    request: HttpRequest | None = None,
) -> Campaign:
    """
    Validate, materialize recipients, and hand the campaign to the sender.

    The dispatcher is required up front so that a campaign is never moved to
    PROCESSING when nothing is able to process it.
    """
    if not campaign.can_launch:
        raise InvalidStateTransition(
            f"A {campaign.get_status_display().lower()} campaign cannot be launched."
        )

    # An unconfirmed address is the one account-level thing that blocks a send.
    # Verification is not a login gate — being locked out of an empty dashboard
    # helps nobody — but sending to real people from an address that may not
    # exist is different: the failure notices and the replies would go nowhere.
    if user is not None and not getattr(user, "email_verified", True):
        raise ValidationFailed(
            "Confirm your email address before sending a campaign.",
            details={
                "blockers": [
                    f"We sent a confirmation link to {user.email}. "
                    "Click it, or request a new one from your profile."
                ]
            },
        )

    blockers = validation_blockers(campaign)
    if blockers:
        raise ValidationFailed(
            "This campaign is not ready to send.", details={"blockers": blockers}
        )

    # Raises SendingUnavailable before any state change — this checks the
    # queue is actually reachable, not merely that a sender is registered.
    dispatch.preflight()

    recipients = list(resolve_audience(campaign).order_by("name"))
    created = materialize_messages(campaign, recipients)

    campaign.total_recipients = created
    campaign.started_at = timezone.now()
    campaign.failure_reason = ""
    transition(campaign, CampaignStatus.PROCESSING, save=False)
    campaign.save(
        update_fields=["status", "total_recipients", "started_at", "failure_reason", "updated_at"]
    )

    record_audit(
        AuditAction.CAMPAIGN_LAUNCHED,
        user=user,
        request=request,
        obj=campaign,
        description=f"Launched campaign {campaign.name} to {created} recipient(s)",
        metadata={
            "recipients": created,
            "template": campaign.template.name if campaign.template else "",
            "groups": list(campaign.audience_groups.values_list("name", flat=True)),
            "target_all_eligible": campaign.target_all_eligible,
        },
    )

    # Queue only once the transaction has committed, so a worker can never
    # pick up a message row that is not yet visible to it.
    transaction.on_commit(lambda: dispatch.dispatch_campaign(campaign))

    logger.info("Campaign %s launched to %d recipients", campaign.pk, created)
    return campaign


@transaction.atomic
def pause_campaign(campaign: Campaign, *, user=None, request: HttpRequest | None = None) -> Campaign:
    transition(campaign, CampaignStatus.PAUSED)
    record_audit(
        AuditAction.CAMPAIGN_PAUSED,
        user=user,
        request=request,
        obj=campaign,
        description=f"Paused campaign {campaign.name}",
    )
    return campaign


@transaction.atomic
def resume_campaign(
    campaign: Campaign, *, user=None, request: HttpRequest | None = None
) -> Campaign:
    dispatch.preflight()
    transition(campaign, CampaignStatus.PROCESSING)
    record_audit(
        AuditAction.CAMPAIGN_RESUMED,
        user=user,
        request=request,
        obj=campaign,
        description=f"Resumed campaign {campaign.name}",
    )
    transaction.on_commit(lambda: dispatch.dispatch_campaign(campaign))
    return campaign


@transaction.atomic
def cancel_campaign(
    campaign: Campaign, *, user=None, request: HttpRequest | None = None
) -> Campaign:
    """
    Cancel a campaign and abandon anything not yet sent.

    Messages already handed to the provider cannot be recalled; only rows that
    have not left the queue are marked failed.
    """
    from messaging.models import IN_FLIGHT_STATUSES, Message, MessageStatus

    transition(campaign, CampaignStatus.CANCELLED, save=False)
    campaign.completed_at = timezone.now()
    campaign.save(update_fields=["status", "completed_at", "updated_at"])

    abandoned = Message.objects.filter(
        campaign=campaign, status__in=IN_FLIGHT_STATUSES
    ).update(
        status=MessageStatus.FAILED,
        error_code="cancelled",
        error_message="Campaign cancelled before this message was sent.",
        failed_at=timezone.now(),
    )

    record_audit(
        AuditAction.CAMPAIGN_CANCELLED,
        user=user,
        request=request,
        obj=campaign,
        description=f"Cancelled campaign {campaign.name}",
        metadata={"abandoned_messages": abandoned},
    )
    return campaign


def finalize_if_complete(campaign: Campaign) -> bool:
    """
    Mark a processing campaign complete once no message is still in flight.

    Called by the worker after each send in Phase 5. Returns True if the
    campaign was finalized by this call.
    """
    from messaging.models import Message

    if campaign.status != CampaignStatus.PROCESSING:
        return False

    if Message.objects.filter(campaign=campaign).in_flight().exists():
        return False

    campaign.completed_at = timezone.now()
    transition(campaign, CampaignStatus.COMPLETED, save=False)
    campaign.save(update_fields=["status", "completed_at", "updated_at"])
    logger.info("Campaign %s completed", campaign.pk)
    return True
