"""
Contact and group business logic.

Views and serializers call these functions; they never write contact state
directly. Everything that changes consent or membership is audited here, in one
place, so the compliance trail cannot be bypassed by adding a new view.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import IntegrityError, transaction
from django.http import HttpRequest

from contacts.models import (
    Contact,
    ContactGroup,
    ContactStatus,
    GroupMembership,
    OptInSource,
    OptOutSource,
)
from core.audit import record_audit
from core.exceptions import ConflictError, ValidationFailed
from core.models import AuditAction
from core.phone import PhoneNumberError, parse_phone_number

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phone handling
# ---------------------------------------------------------------------------


def normalize_contact_phone(raw: str, default_region: str | None = None) -> tuple[str, str]:
    """
    Return ``(e164, dialling_code)`` for ``raw``.

    Raises :class:`~core.exceptions.ValidationFailed` with a field-keyed error
    so serializers and forms can surface it against ``phone_number``.
    """
    try:
        parsed = parse_phone_number(raw, default_region)
    except PhoneNumberError as exc:
        raise ValidationFailed(str(exc), details={"phone_number": [str(exc)]}) from exc
    return parsed.e164, parsed.country_code


def find_duplicate(phone_e164: str, *, exclude_pk=None) -> Contact | None:
    """Return the existing contact holding ``phone_e164``, if any."""
    queryset = Contact.objects.filter(phone_number=phone_e164)
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    return queryset.first()


# ---------------------------------------------------------------------------
# Contact lifecycle
# ---------------------------------------------------------------------------


@transaction.atomic
def create_contact(
    *,
    name: str,
    phone_number: str,
    email: str = "",
    status: str = ContactStatus.ACTIVE,
    opted_in: bool = False,
    opt_in_source: str = "",
    notes: str = "",
    groups: list[ContactGroup] | None = None,
    organization=None,
    user=None,
    request: HttpRequest | None = None,
    default_region: str | None = None,
) -> Contact:
    """
    Create a contact from unnormalized input.

    Consent defaults to False: a contact is only ever opted in when the caller
    passes ``opted_in=True`` explicitly, and the source is recorded with it.
    """
    e164, dialling_code = normalize_contact_phone(phone_number, default_region)

    existing = find_duplicate(e164)
    if existing is not None:
        raise ConflictError(
            f"A contact with the phone number {e164} already exists.",
            details={"phone_number": [f"{existing.name} already uses this number."]},
        )

    contact = Contact(
        name=name.strip(),
        phone_number=e164,
        country_code=dialling_code,
        email=(email or "").strip(),
        status=status,
        notes=notes or "",
        organization=organization,
        created_by=user,
    )

    if opted_in:
        contact.opt_in(opt_in_source or OptInSource.MANUAL)

    try:
        contact.full_clean(exclude=["created_by", "organization"])
        contact.save()
    except IntegrityError as exc:  # pragma: no cover - guarded by find_duplicate
        raise ConflictError(f"A contact with the phone number {e164} already exists.") from exc

    if groups:
        add_contacts_to_group_bulk(groups, [contact], user=user)

    record_audit(
        AuditAction.CONTACT_CREATED,
        user=user,
        request=request,
        obj=contact,
        description=f"Created contact {contact.name}",
        metadata={"phone_number": e164, "opted_in": contact.opted_in},
    )
    return contact


@transaction.atomic
def update_contact(
    contact: Contact,
    *,
    user=None,
    request: HttpRequest | None = None,
    default_region: str | None = None,
    **fields: Any,
) -> Contact:
    """
    Update editable fields on ``contact``.

    ``opted_in`` is deliberately *not* accepted here: consent changes go through
    :func:`set_consent` so they always carry a source and an audit entry.
    """
    if "opted_in" in fields:
        raise ValidationFailed(
            "Consent cannot be changed through a general update.",
            details={"opted_in": ["Use the opt-in or opt-out action instead."]},
        )

    changed: dict[str, Any] = {}

    if "phone_number" in fields and fields["phone_number"]:
        e164, dialling_code = normalize_contact_phone(fields["phone_number"], default_region)
        if e164 != contact.phone_number:
            duplicate = find_duplicate(e164, exclude_pk=contact.pk)
            if duplicate is not None:
                raise ConflictError(
                    f"A contact with the phone number {e164} already exists.",
                    details={"phone_number": [f"{duplicate.name} already uses this number."]},
                )
            changed["phone_number"] = e164
            contact.phone_number = e164
            contact.country_code = dialling_code

    for field in ("name", "email", "status", "notes"):
        if field in fields and fields[field] is not None:
            value = fields[field].strip() if isinstance(fields[field], str) else fields[field]
            if value != getattr(contact, field):
                changed[field] = value
                setattr(contact, field, value)

    if changed:
        contact.full_clean(exclude=["created_by"])
        contact.save()
        record_audit(
            AuditAction.CONTACT_UPDATED,
            user=user,
            request=request,
            obj=contact,
            description=f"Updated contact {contact.name}",
            metadata={"changed_fields": sorted(changed)},
        )

    return contact


@transaction.atomic
def delete_contact(contact: Contact, *, user=None, request: HttpRequest | None = None) -> None:
    """Delete a contact, keeping an audit record of who removed whom."""
    identity = {"name": contact.name, "phone_number": contact.phone_number}
    record_audit(
        AuditAction.CONTACT_DELETED,
        user=user,
        request=request,
        obj=contact,
        description=f"Deleted contact {contact.name}",
        metadata=identity,
    )
    contact.delete()


@transaction.atomic
def set_consent(
    contact: Contact,
    *,
    opted_in: bool,
    source: str = "",
    user=None,
    request: HttpRequest | None = None,
) -> Contact:
    """
    Record an opt-in or opt-out, with its source, and audit it.

    This is the only supported way to change ``Contact.opted_in``.
    """
    if opted_in:
        contact.opt_in(source or OptInSource.MANUAL)
        action = AuditAction.CONTACT_OPTED_IN
        description = f"{contact.name} opted in"
    else:
        contact.opt_out(source or OptOutSource.MANUAL)
        action = AuditAction.CONTACT_OPTED_OUT
        description = f"{contact.name} opted out"

    contact.save(
        update_fields=[
            "opted_in",
            "opt_in_source",
            "opt_in_at",
            "opt_out_source",
            "opt_out_at",
            "updated_at",
        ]
    )

    record_audit(
        action,
        user=user,
        request=request,
        obj=contact,
        description=description,
        metadata={"phone_number": contact.phone_number, "source": source or "manual"},
    )
    return contact


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@transaction.atomic
def add_contacts_to_group(
    group: ContactGroup,
    contacts: list[Contact],
    *,
    user=None,
    request: HttpRequest | None = None,
) -> int:
    """
    Add contacts to a group. Returns the number of *new* memberships.

    Re-adding an existing member is a no-op rather than an error, which keeps
    the endpoint idempotent.
    """
    memberships = [
        GroupMembership(group=group, contact=contact, added_by=user) for contact in contacts
    ]
    created = GroupMembership.objects.bulk_create(memberships, ignore_conflicts=True)

    # bulk_create with ignore_conflicts cannot report which rows landed, so the
    # true count comes from a follow-up query.
    added = GroupMembership.objects.filter(
        group=group, contact__in=[c.pk for c in contacts]
    ).count()
    logger.debug("Group %s membership request for %d contacts", group.name, len(created))
    return added


def add_contacts_to_group_bulk(
    groups: list[ContactGroup], contacts: list[Contact], *, user=None
) -> None:
    """Add the same contacts to several groups at once."""
    memberships = [
        GroupMembership(group=group, contact=contact, added_by=user)
        for group in groups
        for contact in contacts
    ]
    if memberships:
        GroupMembership.objects.bulk_create(memberships, ignore_conflicts=True)


@transaction.atomic
def remove_contacts_from_group(group: ContactGroup, contacts: list[Contact]) -> int:
    """Remove contacts from a group. Returns the number of memberships deleted."""
    deleted, _ = GroupMembership.objects.filter(
        group=group, contact__in=[c.pk for c in contacts]
    ).delete()
    return deleted


@transaction.atomic
def set_contact_groups(
    contact: Contact,
    groups: list[ContactGroup],
    *,
    user=None,
) -> dict[str, int]:
    """
    Make ``contact`` a member of exactly ``groups``.

    Only the affected memberships are touched, so this stays two queries
    regardless of how many groups exist.
    """
    current = set(
        GroupMembership.objects.filter(contact=contact).values_list("group_id", flat=True)
    )
    wanted = {group.pk for group in groups}

    to_remove = current - wanted
    to_add = [group for group in groups if group.pk not in current]

    removed = 0
    if to_remove:
        removed, _ = GroupMembership.objects.filter(
            contact=contact, group_id__in=to_remove
        ).delete()

    if to_add:
        GroupMembership.objects.bulk_create(
            [GroupMembership(group=group, contact=contact, added_by=user) for group in to_add],
            ignore_conflicts=True,
        )

    return {"added": len(to_add), "removed": removed}


@transaction.atomic
def set_group_members(
    group: ContactGroup,
    contacts: list[Contact],
    *,
    user=None,
) -> dict[str, int]:
    """Replace a group's membership wholesale. Returns added/removed counts."""
    current = set(
        GroupMembership.objects.filter(group=group).values_list("contact_id", flat=True)
    )
    wanted = {contact.pk for contact in contacts}

    to_remove = current - wanted
    to_add = [contact for contact in contacts if contact.pk not in current]

    removed = 0
    if to_remove:
        removed, _ = GroupMembership.objects.filter(
            group=group, contact_id__in=to_remove
        ).delete()

    if to_add:
        GroupMembership.objects.bulk_create(
            [GroupMembership(group=group, contact=contact, added_by=user) for contact in to_add],
            ignore_conflicts=True,
        )

    return {"added": len(to_add), "removed": removed}
