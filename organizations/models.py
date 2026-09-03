"""
The tenant boundary.

Everything a customer owns hangs off an :class:`Organization`, and the whole
point of this app is that one customer can never reach another's contacts,
campaigns or messages. That guarantee is only as good as its weakest query, so
the enforcement lives in :mod:`organizations.scoping` rather than being
re-typed at each call site.

**Two kinds of role, deliberately separate.** ``OrganizationMember.role`` says
what somebody may do *inside a business* — an owner can change billing, a
member can send campaigns. ``User.role`` and ``is_staff`` say what somebody is
to *the platform*. Conflating them is how a customer's admin ends up able to
see another customer's data.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


class OrganizationStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    SUSPENDED = "suspended", _("Suspended")
    CLOSED = "closed", _("Closed")


class OrganizationRole(models.TextChoices):
    """A member's authority within one organization."""

    OWNER = "owner", _("Owner")
    ADMIN = "admin", _("Administrator")
    MEMBER = "member", _("Member")


class OrganizationQuerySet(models.QuerySet):
    def active(self) -> OrganizationQuerySet:
        return self.filter(status=OrganizationStatus.ACTIVE)

    def for_user(self, user) -> OrganizationQuerySet:
        """Organizations this user belongs to. The basis of every scoped query."""
        if not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(memberships__user=user).distinct()


class Organization(BaseModel):
    """A customer business. The unit of ownership, billing and isolation."""

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160, unique=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_organizations",
        help_text=_("The account ultimately responsible for this organization."),
    )
    timezone = models.CharField(
        max_length=64,
        default="UTC",
        help_text=_("Used when presenting dates and grouping reports."),
    )
    status = models.CharField(
        max_length=16,
        choices=OrganizationStatus.choices,
        default=OrganizationStatus.ACTIVE,
        db_index=True,
    )

    objects = OrganizationQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name = _("organization")
        verbose_name_plural = _("organizations")
        indexes = [models.Index(fields=["status", "name"], name="org_status_name_idx")]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._unique_slug()
        super().save(*args, **kwargs)

    def _unique_slug(self) -> str:
        """
        A slug nobody else holds.

        Two businesses genuinely can share a name, and the slug is unique, so
        a collision has to resolve rather than raise at save time.
        """
        base = slugify(self.name) or "organization"
        candidate = base
        suffix = 2
        while Organization.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate[:160]

    @property
    def is_active(self) -> bool:
        return self.status == OrganizationStatus.ACTIVE

    def member_for(self, user) -> OrganizationMember | None:
        if not getattr(user, "is_authenticated", False):
            return None
        return self.memberships.filter(user=user).first()

    def has_member(self, user) -> bool:
        return self.member_for(user) is not None


class OrganizationMember(BaseModel):
    """
    A user's seat in an organization.

    Modelled as its own row rather than a foreign key on the user because the
    same person may belong to more than one business — an agency running
    campaigns for several clients is the obvious case, and retrofitting that
    later would mean moving every scoped query again.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(
        max_length=16, choices=OrganizationRole.choices, default=OrganizationRole.MEMBER
    )

    class Meta:
        ordering = ["organization__name", "user__email"]
        verbose_name = _("organization member")
        verbose_name_plural = _("organization members")
        constraints = [
            # One seat per person per organization; the role lives on the seat.
            models.UniqueConstraint(
                fields=["organization", "user"], name="unique_member_per_organization"
            ),
        ]
        indexes = [models.Index(fields=["user", "organization"], name="member_lookup_idx")]

    def __str__(self) -> str:
        return f"{self.user} in {self.organization} ({self.get_role_display()})"

    @property
    def is_owner(self) -> bool:
        return self.role == OrganizationRole.OWNER

    @property
    def can_administer(self) -> bool:
        """Owners and administrators may change billing and manage members."""
        return self.role in {OrganizationRole.OWNER, OrganizationRole.ADMIN}
