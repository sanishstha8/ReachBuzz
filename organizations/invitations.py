"""
Bringing a colleague into an organization.

Three states, and the model only stores two of them: an invitation is
``PENDING`` until somebody acts on it, then ``ACCEPTED`` or ``REVOKED``, and
"expired" is computed rather than stored — see ``Invitation.is_expired``.

**An invitation does not create a user.** Sending one is not consent to
anything from the recipient, so nothing about them is created until they
follow the link and prove they hold the address. If they already have an
account, accepting adds a seat to it. If they do not, accepting is a small
registration — and the act of accepting *is* the email confirmation, because
following a link mailed to an address is exactly what Stage 2's verification
already treats as proof of holding it.

**A seat is checked twice.** Once when the invitation is sent, so an
organization at its limit is told immediately rather than after making someone
wait for an email that was never going to work. Once again when it is
accepted, because a plan can change or other seats can fill in the time
between — and accepting is the moment a seat is actually spent, so that is the
check that has to be right even if the first one was skipped or stale.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from billing import usage
from core.audit import record_audit
from core.exceptions import ConflictError, NotAllowed, ValidationFailed
from core.models import AuditAction
from organizations.models import (
    INVITATION_EXPIRY_DAYS,
    Invitation,
    InvitationStatus,
    OrganizationMember,
    OrganizationRole,
)

logger = logging.getLogger(__name__)

User = get_user_model()

#: Roles an invitation may offer. Never OWNER — ownership is not handed out by
#: email, and there is no path in this module that will assign it.
INVITABLE_ROLES = (OrganizationRole.ADMIN, OrganizationRole.MEMBER)


def _seat_count(organization) -> int:
    """
    Members plus open invitations.

    A limit checked against members alone would let an organization at its
    ceiling send unlimited invitations, all of which would then race each
    other at acceptance. Counting the invitations too makes the send-time
    check mean something, even though acceptance is where a seat is actually
    spent and re-checked.
    """
    return OrganizationMember.objects.filter(organization=organization).count() + (
        Invitation.objects.filter(organization=organization).pending().count()
    )


@transaction.atomic
def invite(
    organization,
    *,
    email: str,
    role: str = OrganizationRole.MEMBER,
    invited_by=None,
    request=None,
) -> Invitation:
    """
    Offer somebody a seat.

    Resends in place rather than creating a second row when one is already
    pending for this address — the unique constraint is the backstop, this is
    the ordinary path to the same outcome.
    """
    email = email.strip().lower()

    if role not in INVITABLE_ROLES:
        raise ValidationFailed(
            "Invitations can only offer Administrator or Member.",
            details={"role": ["Ownership is not sent by email."]},
        )

    if OrganizationMember.objects.filter(
        organization=organization, user__email__iexact=email
    ).exists():
        raise ConflictError(
            f"{email} is already a member of {organization.name}.",
            details={"email": ["This address already belongs to the organization."]},
        )

    # Checked against members *and* pending invitations, not usage.check()'s
    # ordinary member count — otherwise an organization at its ceiling could
    # send unlimited invitations, all of which would then race each other at
    # acceptance. Written before anything else, with the seat this invitation
    # would eventually spend, so the refusal happens before any mail goes out.
    plan = usage.plan_for(organization)
    if plan is not None:
        used = _seat_count(organization)
        if not plan.allows("max_team_members", used, additional=1):
            limit = plan.limit("max_team_members")
            raise usage.QuotaExceeded(
                f"This would go past the {plan.name} team limit.",
                details={
                    "blockers": [
                        f"{organization.name} has {used} of {limit} team members and "
                        "pending invitations. Upgrade the plan, or revoke a pending "
                        "invitation first."
                    ],
                    "metric": "max_team_members",
                    "used": used,
                    "limit": limit,
                },
            )

    existing = Invitation.objects.filter(
        organization=organization, email=email, status=InvitationStatus.PENDING
    ).first()

    invitation, created = (
        (existing, False)
        if existing is not None
        else (Invitation(organization=organization, email=email), True)
    )
    invitation.role = role
    invitation.invited_by = invited_by
    invitation.expires_at = timezone.now() + timezone.timedelta(days=INVITATION_EXPIRY_DAYS)
    invitation.save()

    send_invitation_email(invitation, request=request)

    record_audit(
        AuditAction.MEMBER_INVITED,
        user=invited_by,
        request=request,
        obj=organization,
        description=f"Invited {email} as {invitation.get_role_display()}",
        metadata={"email": email, "role": role, "resent": not created},
    )
    logger.info(
        "%s %s to organization %s",
        "Resent invitation" if not created else "Invited",
        email,
        organization.pk,
    )
    return invitation


def send_invitation_email(invitation: Invitation, *, request=None) -> None:
    """
    Mail the link. Failures are logged and swallowed.

    Matches ``accounts.registration.send_verification_email``: the invitation
    already exists and is real, so a mail server being briefly unreachable
    must not look like the invitation itself failed. Resending is always
    available from the members page.
    """
    from django.urls import reverse

    path = reverse("organizations:invitation-accept", kwargs={"token": invitation.token})
    context = {
        "invitation": invitation,
        "organization": invitation.organization,
        "accept_url": request.build_absolute_uri(path) if request else path,
        "brand_name": settings.SITE_NAME,
        "inviter": invitation.invited_by,
    }

    try:
        send_mail(
            subject=(
                f"You have been invited to join {invitation.organization.name} "
                f"on {settings.SITE_NAME}"
            ),
            message=render_to_string("organizations/email/invitation.txt", context),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[invitation.email],
            fail_silently=False,
        )
    except Exception:  # pragma: no cover - depends on the mail server
        logger.exception("Could not send an invitation email for %s", invitation.pk)


@transaction.atomic
def revoke(invitation: Invitation, *, user=None, request=None) -> Invitation:
    """Close an invitation before it is accepted. Revoking twice is a no-op."""
    if invitation.status != InvitationStatus.PENDING:
        return invitation

    invitation.status = InvitationStatus.REVOKED
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=["status", "revoked_at"])

    record_audit(
        AuditAction.INVITATION_REVOKED,
        user=user,
        request=request,
        obj=invitation.organization,
        description=f"Revoked the invitation to {invitation.email}",
        metadata={"email": invitation.email},
    )
    return invitation


def find_open(token: str) -> Invitation | None:
    """
    The invitation a link points at, if it still leads anywhere.

    One lookup either the accept page or the accept action can share, so the
    two cannot disagree about whether a link is still good.
    """
    invitation = Invitation.objects.select_related("organization").filter(token=token).first()
    if invitation is None or not invitation.is_open:
        return None
    return invitation


@transaction.atomic
def accept(
    invitation: Invitation,
    *,
    user=None,
    first_name: str = "",
    last_name: str = "",
    password: str = "",
    request=None,
) -> OrganizationMember:
    """
    Turn an invitation into a seat.

    Exactly one of two shapes: ``user`` is an already-authenticated person
    whose address matches the invitation, or ``first_name``/``last_name``/
    ``password`` describe a new account to create for it. The form and view
    decide which; this function trusts whichever it is given.

    Marks the new or existing user's email verified. Reaching this line means
    they followed a link mailed to ``invitation.email`` — the same proof of
    holding an address that Stage 2's confirmation link is built on — so a
    second confirmation would ask them to prove something accepting the
    invitation already proved.
    """
    if not invitation.is_open:
        raise ValidationFailed(
            "This invitation is no longer open.",
            details={"invitation": ["It may have been used, revoked, or it has expired."]},
        )

    if user is not None and user.email.lower() != invitation.email.lower():
        raise NotAllowed(
            "This invitation was sent to a different address.",
            details={"email": [f"Sign in as {invitation.email} to accept it."]},
        )

    if user is None:
        user = User.objects.create_user(
            email=invitation.email,
            password=password,
            first_name=first_name.strip(),
            last_name=last_name.strip(),
        )

    if not user.email_verified:
        user.mark_email_verified()

    # Re-checked here, not trusted from send time: a plan can change or other
    # invitations can be accepted in between, and this is the moment a seat is
    # actually spent.
    usage.check(invitation.organization, "max_team_members", additional=1)

    member, _created = OrganizationMember.objects.get_or_create(
        organization=invitation.organization,
        user=user,
        defaults={"role": invitation.role},
    )

    invitation.status = InvitationStatus.ACCEPTED
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["status", "accepted_at"])

    record_audit(
        AuditAction.INVITATION_ACCEPTED,
        user=user,
        request=request,
        obj=invitation.organization,
        description=f"{user.email} joined as {member.get_role_display()}",
        metadata={"role": member.role},
    )
    logger.info(
        "User %s accepted an invitation to organization %s", user.pk, invitation.organization_id
    )
    return member


@transaction.atomic
def remove_member(member: OrganizationMember, *, removed_by=None, request=None) -> None:
    """
    Take a seat back.

    Refuses to remove the last owner. An organization with no owner cannot be
    administered by anyone — the same state Stage 2's registration transaction
    exists to make unreachable, reached here from the other direction.
    """
    if member.is_owner:
        remaining_owners = OrganizationMember.objects.filter(
            organization=member.organization, role=OrganizationRole.OWNER
        ).exclude(pk=member.pk)
        if not remaining_owners.exists():
            raise ValidationFailed(
                "An organization must keep at least one owner.",
                details={"member": ["Make somebody else an owner first."]},
            )

    organization = member.organization
    email = member.user.email

    record_audit(
        AuditAction.MEMBER_REMOVED,
        user=removed_by,
        request=request,
        obj=organization,
        description=f"Removed {email} from {organization.name}",
        metadata={"email": email, "role": member.role},
    )
    member.delete()
