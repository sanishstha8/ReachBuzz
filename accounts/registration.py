"""
Self-service sign-up.

Registering is three things at once — an account, a business, and that
account's ownership of it — and a customer with any one of them missing is
broken in a way that is tedious to repair by hand. So it happens in one
transaction: either they get all three or the address stays free to try again.

**Verification uses Django's signed tokens, not a table.** The same machinery
that backs password reset already gives one-time, time-limited links tied to
the user's current state, and it invalidates itself once the thing it proves
has happened. A ``VerificationToken`` model would be a second implementation
of that with its own expiry bugs and its own rows to clean up.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from core.audit import record_audit
from core.models import AuditAction
from organizations.models import Organization, OrganizationMember, OrganizationRole

logger = logging.getLogger(__name__)

User = get_user_model()


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    """
    Tokens for confirming an address.

    Its own generator rather than the shared one so that a password-reset link
    cannot verify an address and a verification link cannot reset a password —
    the hash includes ``email_verified``, so a token also stops working the
    moment it has been used.
    """

    def _make_hash_value(self, user, timestamp: int) -> str:
        return f"{user.pk}{user.email}{user.email_verified}{timestamp}"


verification_token = EmailVerificationTokenGenerator()


@transaction.atomic
def register(
    *,
    email: str,
    password: str,
    organization_name: str,
    first_name: str = "",
    last_name: str = "",
    phone: str = "",
    request=None,
) -> tuple[object, Organization]:
    """
    Create an account, its business, and the owner's seat in it.

    Atomic on purpose: a user with no organization can sign in and see nothing,
    and an organization with no owner cannot be administered at all. Neither
    half is a state worth being able to reach.
    """
    user = User.objects.create_user(
        email=email,
        password=password,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        phone=phone.strip(),
    )

    organization = Organization.objects.create(name=organization_name.strip(), owner=user)
    OrganizationMember.objects.create(
        organization=organization, user=user, role=OrganizationRole.OWNER
    )

    record_audit(
        AuditAction.USER_REGISTERED,
        user=user,
        request=request,
        obj=organization,
        description=f"Registered {organization.name}",
        metadata={"organization": str(organization.pk)},
    )
    logger.info("Registered %s for organization %s", user.pk, organization.pk)
    return user, organization


def send_verification_email(user, request=None) -> None:
    """
    Send the confirmation link.

    Failures are logged and swallowed. A registration that already succeeded
    must not appear to fail because a mail server was briefly unreachable —
    the account exists, and the link can be requested again.
    """
    path = reverse(
        "accounts:verify-email",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": verification_token.make_token(user),
        },
    )
    context = {
        "user": user,
        "verify_url": request.build_absolute_uri(path) if request else path,
        "brand_name": settings.SITE_NAME,
    }

    try:
        send_mail(
            subject=f"Confirm your email address for {settings.SITE_NAME}",
            message=render_to_string("accounts/email/verify_email.txt", context),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception:  # pragma: no cover - depends on the mail server
        logger.exception("Could not send a verification email to user %s", user.pk)


def verify(uidb64: str, token: str):
    """
    Confirm an address from a link. Returns the user, or None.

    One return value for every kind of failure — a malformed id, an unknown
    user, an expired or already-used token — because telling a stranger which
    it was tells them whether an address is registered.
    """
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uidb64)))
    except (TypeError, ValueError, OverflowError, User.DoesNotExist, ValidationError):
        return None

    if not verification_token.check_token(user, token):
        return None

    user.mark_email_verified()
    return user
