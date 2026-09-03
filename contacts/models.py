"""
Contacts, groups and CSV import records.

Consent is modelled as state, not as a convenience flag: ``opted_in`` always
travels with a source and a timestamp so the business can show *how* a
recipient came to be on the list. ``Contact.objects.eligible()`` is the single
definition of "may receive a campaign", and campaign audience resolution has
no way to opt out of it.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel
from core.phone import validate_phone_number
from organizations.scoping import OrganizationOwnedModel, OrganizationScopedQuerySet


class ContactStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    INACTIVE = "inactive", _("Inactive")
    INVALID = "invalid", _("Invalid number")
    BLOCKED = "blocked", _("Blocked")


class OptInSource(models.TextChoices):
    """How consent was obtained. Required whenever ``opted_in`` becomes True."""

    MANUAL = "manual", _("Entered manually by an operator")
    CSV_IMPORT = "csv_import", _("CSV import with explicit consent column")
    WEB_FORM = "web_form", _("Sign-up form")
    INBOUND_MESSAGE = "inbound_message", _("Recipient messaged us first")
    UNKNOWN = "unknown", _("Not recorded")


class OptOutSource(models.TextChoices):
    MANUAL = "manual", _("Removed by an operator")
    INBOUND_STOP = "inbound_stop", _("Recipient replied STOP")
    CSV_IMPORT = "csv_import", _("CSV import without consent")
    PROVIDER = "provider", _("Reported by the messaging provider")


class ContactQuerySet(OrganizationScopedQuerySet):
    def opted_in(self) -> ContactQuerySet:
        return self.filter(opted_in=True)

    def active(self) -> ContactQuerySet:
        return self.filter(status=ContactStatus.ACTIVE)

    def eligible(self) -> ContactQuerySet:
        """
        Contacts a campaign is permitted to message.

        This is the only place the rule is written. Campaign audience
        resolution calls it and offers no override.
        """
        return self.filter(opted_in=True, status=ContactStatus.ACTIVE)

    def search(self, term: str) -> ContactQuerySet:
        term = (term or "").strip()
        if not term:
            return self
        return self.filter(
            models.Q(name__icontains=term)
            | models.Q(phone_number__icontains=term)
            | models.Q(email__icontains=term)
        )

    def in_group(self, group) -> ContactQuerySet:
        return self.filter(group_memberships__group=group)


class Contact(OrganizationOwnedModel, BaseModel):
    """A person who may receive WhatsApp messages, and their consent state."""

    name = models.CharField(max_length=150, db_index=True)
    phone_number = models.CharField(
        max_length=20,
        unique=True,
        validators=[validate_phone_number],
        help_text=_("Stored in E.164 format, e.g. +9779800000000."),
    )
    country_code = models.CharField(
        max_length=5,
        blank=True,
        help_text=_("Dialling code derived from the phone number, e.g. 977."),
    )
    email = models.EmailField(blank=True)

    status = models.CharField(
        max_length=16,
        choices=ContactStatus.choices,
        default=ContactStatus.ACTIVE,
        db_index=True,
    )

    # --- Consent -----------------------------------------------------------
    opted_in = models.BooleanField(
        default=False,
        db_index=True,
        help_text=_("Only opted-in contacts can be included in a campaign."),
    )
    opt_in_source = models.CharField(max_length=32, choices=OptInSource.choices, blank=True)
    opt_in_at = models.DateTimeField(null=True, blank=True)
    opt_out_source = models.CharField(max_length=32, choices=OptOutSource.choices, blank=True)
    opt_out_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    # Last provider-reported delivery problem, surfaced in the contact detail
    # page so an operator can see why a number keeps failing.
    last_error_code = models.CharField(max_length=32, blank=True)
    last_error_message = models.CharField(max_length=255, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_contacts",
    )

    objects = ContactQuerySet.as_manager()

    class Meta:
        ordering = ["name", "phone_number"]
        verbose_name = _("contact")
        verbose_name_plural = _("contacts")
        indexes = [
            models.Index(fields=["opted_in", "status"], name="contact_eligibility_idx"),
            models.Index(fields=["-created_at"], name="contact_recent_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(phone_number__startswith="+"),
                name="contact_phone_is_e164",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} <{self.phone_number}>"

    @property
    def is_eligible(self) -> bool:
        """Mirrors ``ContactQuerySet.eligible`` for a single instance."""
        return self.opted_in and self.status == ContactStatus.ACTIVE

    @property
    def group_names(self) -> list[str]:
        return [membership.group.name for membership in self.group_memberships.all()]

    def opt_in(self, source: str = OptInSource.MANUAL, *, when=None) -> None:
        """Record consent. Does not save; callers control the transaction."""
        self.opted_in = True
        self.opt_in_source = source
        self.opt_in_at = when or timezone.now()
        self.opt_out_source = ""
        self.opt_out_at = None

    def opt_out(self, source: str = OptOutSource.MANUAL, *, when=None) -> None:
        """Withdraw consent. Does not save; callers control the transaction."""
        self.opted_in = False
        self.opt_out_source = source
        self.opt_out_at = when or timezone.now()


class ContactGroupQuerySet(OrganizationScopedQuerySet):
    def with_counts(self) -> ContactGroupQuerySet:
        """
        Annotate ``member_count`` and ``eligible_count``.

        Both numbers matter when planning a campaign: the first is how big the
        list is, the second is how many of them consent allows us to message.
        Annotating avoids a query per row when rendering the group table.
        """
        return self.annotate(
            member_count=models.Count("memberships", distinct=True),
            eligible_count=models.Count(
                "memberships",
                filter=models.Q(
                    memberships__contact__opted_in=True,
                    memberships__contact__status=ContactStatus.ACTIVE,
                ),
                distinct=True,
            ),
        )


class ContactGroup(OrganizationOwnedModel, BaseModel):
    """A named list of contacts, used as a campaign audience."""

    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    contacts = models.ManyToManyField(
        Contact,
        through="GroupMembership",
        related_name="groups",
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_groups",
    )

    objects = ContactGroupQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name = _("contact group")
        verbose_name_plural = _("contact groups")

    def __str__(self) -> str:
        return self.name

    # Counts are normally supplied by ``ContactGroupQuerySet.with_counts()``.
    # These single-instance fallbacks exist for the cases where an unannotated
    # object is all that is to hand; they are deliberately not properties named
    # after the annotations, which would shadow them and fail on assignment.

    def count_members(self) -> int:
        return self.memberships.count()

    def count_eligible(self) -> int:
        """Members who could actually be messaged right now."""
        return Contact.objects.eligible().in_group(self).count()


class GroupMembership(models.Model):
    """
    Explicit through-model so membership carries its own timestamp.

    A contact can belong to many groups; the unique constraint makes adding an
    existing member idempotent rather than an error.
    """

    contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="group_memberships"
    )
    group = models.ForeignKey(
        ContactGroup, on_delete=models.CASCADE, related_name="memberships"
    )
    added_at = models.DateTimeField(auto_now_add=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="group_additions",
    )

    class Meta:
        ordering = ["-added_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "group"], name="unique_contact_per_group"
            ),
        ]
        indexes = [models.Index(fields=["group", "contact"], name="membership_lookup_idx")]

    def __str__(self) -> str:
        return f"{self.contact.name} in {self.group.name}"


class ImportStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    PROCESSING = "processing", _("Processing")
    COMPLETED = "completed", _("Completed")
    FAILED = "failed", _("Failed")


class RowOutcome(models.TextChoices):
    """Why a CSV row did or did not become a contact."""

    IMPORTED = "imported", _("Imported")
    UPDATED = "updated", _("Updated existing contact")
    DUPLICATE = "duplicate", _("Duplicate phone number")
    INVALID = "invalid", _("Invalid data")


class ContactImport(OrganizationOwnedModel, BaseModel):
    """
    One CSV upload and its outcome.

    Kept as a record rather than a transient response so an operator can revisit
    exactly which rows were rejected and why, and so the work can be moved onto
    a Celery task without changing the API.
    """

    file_name = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=ImportStatus.choices, default=ImportStatus.PENDING, db_index=True
    )

    total_rows = models.PositiveIntegerField(default=0)
    imported_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    invalid_count = models.PositiveIntegerField(default=0)
    not_opted_in_count = models.PositiveIntegerField(default=0)

    # Group every valid row was added to, if the operator chose one.
    target_group = models.ForeignKey(
        ContactGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="imports",
    )

    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_imports",
    )

    objects = OrganizationScopedQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("contact import")
        verbose_name_plural = _("contact imports")

    def __str__(self) -> str:
        return f"{self.file_name} ({self.get_status_display()})"

    @property
    def processed_count(self) -> int:
        return self.imported_count + self.updated_count + self.duplicate_count + self.invalid_count

    @property
    def success_rate(self) -> float:
        if not self.total_rows:
            return 0.0
        return round((self.imported_count + self.updated_count) / self.total_rows * 100, 1)


class ContactImportRow(models.Model):
    """A rejected or noteworthy row, retained for the import report."""

    contact_import = models.ForeignKey(
        ContactImport, on_delete=models.CASCADE, related_name="rows"
    )
    row_number = models.PositiveIntegerField(help_text=_("1-based, excluding the header row."))
    outcome = models.CharField(max_length=16, choices=RowOutcome.choices, db_index=True)
    raw_data = models.JSONField(default=dict, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name="import_rows"
    )

    class Meta:
        ordering = ["row_number"]
        indexes = [
            models.Index(fields=["contact_import", "outcome"], name="import_row_outcome_idx"),
        ]

    def __str__(self) -> str:
        return f"Row {self.row_number}: {self.get_outcome_display()}"
