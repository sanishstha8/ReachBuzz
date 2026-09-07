"""
Telling customers what the system already knows.

This application has spent nine stages learning things and keeping them to
itself. It knows when a campaign finished and how much of it failed, when a card
was declined, when a monthly quota is nearly gone, and when a customer's
WhatsApp sender stopped verifying. None of that reaches the person it concerns.

Four rules, and the first three are all versions of the same one.

**Nothing is sent that the recipient did not ask for.** Every notification has a
kind, every kind can be turned off, and the defaults are set per kind rather
than "all on" — a delivery report a customer never asked for is not a service,
it is mail they have to unsubscribe from. See
:class:`~core.models.NotificationPreference`.

**Nothing is sent twice.** A campaign that finishes while a worker is retrying
must not produce two emails. Every notification carries a key derived from what
it is about, and the key is unique.

**Nothing is sent to an address nobody confirmed.** Stage 2 established that an
unverified address blocks sending campaigns; it blocks receiving notifications
too, for the same reason — mail to an address that may not exist is at best
noise and at worst somebody else's inbox.

**A failure to notify never fails the thing being notified about.** A campaign
completes whether or not the email about it goes out. The notification is a
consequence of the work, not a step in it.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)


def notify(
    *,
    user,
    kind: str,
    subject: str,
    template: str,
    context: dict | None = None,
    idempotency_key: str = "",
    organization=None,
) -> bool:
    """
    Send one notification, if this user wants it and has not had it already.

    Returns whether anything was sent. ``False`` is the ordinary case, not an
    error — most calls are for somebody who has this kind switched off, or for
    an event that has already been reported.

    Never raises. Every caller is finishing a piece of real work when it reaches
    here, and none of them should fail because a mail server did.
    """
    from core.models import NotificationLog, NotificationPreference

    try:
        if not _is_reachable(user):
            return False

        if not NotificationPreference.objects.wants(user, kind):
            return False

        key = idempotency_key or f"{kind}:{user.pk}:{timezone.now():%Y-%m-%d}"

        try:
            with transaction.atomic():
                entry = NotificationLog.objects.create(
                    user=user,
                    organization=organization,
                    kind=kind,
                    idempotency_key=key,
                    subject=subject[:255],
                )
        except IntegrityError:
            # Already sent. The constraint is the guard rather than a lookup,
            # because two workers finishing the same campaign would both pass a
            # check and both send.
            logger.info("Notification %s already sent; not repeating it", key)
            return False

        body = render_to_string(template, {**(context or {}), "user": user, "brand_name": settings.SITE_NAME})

        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[user.email],
            fail_silently=False,
        )

        entry.sent_at = timezone.now()
        entry.save(update_fields=["sent_at"])
        return True

    except Exception:
        # Deliberately broad. Whatever went wrong — a template that does not
        # exist, a mail server that does not answer — the caller was in the
        # middle of finishing a campaign or recording a payment, and that must
        # not fail because of this.
        logger.exception("Could not send a %s notification to user %s", kind, getattr(user, "pk", None))
        return False


def notify_organization(*, organization, kind: str, subject: str, template: str, **kwargs) -> int:
    """
    Send to everyone who can act on it, which is not everyone in the business.

    Billing problems go to owners and administrators; a member who cannot change
    the card does not need to be told it failed. Delivery reports go to whoever
    turned them on.
    """
    from core.models import NOTIFICATION_KINDS

    recipients = _recipients_for(organization, kind)
    sent = 0

    for user in recipients:
        key = kwargs.pop("idempotency_key", "") or ""
        if notify(
            user=user,
            kind=kind,
            subject=subject,
            template=template,
            organization=organization,
            idempotency_key=f"{key}:{user.pk}" if key else "",
            **kwargs,
        ):
            sent += 1

    if kind not in NOTIFICATION_KINDS:  # pragma: no cover - programming error
        logger.warning("Sent an unknown notification kind %r", kind)

    return sent


def _recipients_for(organization, kind: str):
    """Who in this organization should hear about this kind of thing."""
    from core.models import ADMIN_ONLY_KINDS
    from organizations.models import OrganizationMember, OrganizationRole

    memberships = OrganizationMember.objects.filter(organization=organization).select_related("user")

    if kind in ADMIN_ONLY_KINDS:
        memberships = memberships.filter(
            role__in=[OrganizationRole.OWNER, OrganizationRole.ADMIN]
        )

    return [membership.user for membership in memberships]


def _is_reachable(user) -> bool:
    """
    Whether it is defensible to send this person mail at all.

    An unconfirmed address blocks receiving notifications for the same reason
    Stage 2 made it block sending campaigns: mail to an address nobody has
    confirmed is at best noise, and at worst it is somebody else's inbox.
    """
    if user is None or not getattr(user, "email", ""):
        return False
    if not getattr(user, "is_active", False):
        return False
    return bool(getattr(user, "email_verified", False))
