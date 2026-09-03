"""
CSV contact import.

Consent is the reason this module is careful. A phone number in a spreadsheet
is not permission to message someone, so the importer will **never** infer
opt-in: a row becomes opted-in only when a recognised consent column holds an
explicitly affirmative value. Anything else is imported opted-*out* and counted
separately in the report.

The work is a pure service (:func:`run_import`) that takes an already-created
:class:`~contacts.models.ContactImport` record, so Phase 5 can move it onto a
Celery task without touching any of the logic below.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import IO

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from contacts.models import (
    Contact,
    ContactGroup,
    ContactImport,
    ContactImportRow,
    ContactStatus,
    GroupMembership,
    ImportStatus,
    OptInSource,
    RowOutcome,
)
from core.audit import record_audit
from core.exceptions import ValidationFailed
from core.models import AuditAction
from core.phone import PhoneNumberError, parse_phone_number

logger = logging.getLogger(__name__)


class CsvImportError(ValidationFailed):
    """The file itself is unusable, as opposed to individual rows being bad."""

    code = "csv_import_failed"


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

# Accepted spellings for each logical column. Headers are lowercased and have
# spaces and hyphens folded to underscores before lookup.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "full_name", "contact_name", "customer_name", "first_name"),
    "phone_number": ("phone_number", "phone", "mobile", "mobile_number", "number", "msisdn", "whatsapp"),
    "email": ("email", "email_address", "e_mail"),
    "opted_in": ("opted_in", "opt_in", "optin", "consent", "consented", "subscribed"),
    "notes": ("notes", "note", "comment", "remarks"),
}

REQUIRED_COLUMNS = ("name", "phone_number")

# Values accepted as explicit consent. Anything not in this set means "no".
CONSENT_TRUE_VALUES = frozenset({"true", "yes", "y", "1", "opted_in", "opt_in", "opted-in", "consented", "subscribed"})
CONSENT_FALSE_VALUES = frozenset({"false", "no", "n", "0", "opted_out", "opt_out", "unsubscribed", ""})

MAX_NAME_LENGTH = 150
MAX_EMAIL_LENGTH = 254
SUPPORTED_DELIMITERS = ",;\t|"


@dataclass
class ParsedRow:
    """One CSV line after normalization, valid or not."""

    row_number: int
    raw: dict[str, str]
    name: str = ""
    phone_e164: str = ""
    country_code: str = ""
    email: str = ""
    opted_in: bool = False
    error: str = ""

    @property
    def is_valid(self) -> bool:
        return not self.error


@dataclass
class ImportSummary:
    """Counts rendered in the import report."""

    total_rows: int = 0
    imported: int = 0
    updated: int = 0
    duplicates: int = 0
    invalid: int = 0
    not_opted_in: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# File-level validation
# ---------------------------------------------------------------------------


def validate_upload(uploaded_file: IO, *, max_bytes: int | None = None) -> str:
    """
    Check the upload and return its decoded text.

    Validates the extension, the size, and that the bytes decode. Rejecting
    here — before any parsing — keeps malformed or oversized uploads from ever
    reaching the row loop.
    """
    max_bytes = max_bytes or settings.CSV_IMPORT_MAX_BYTES

    name = getattr(uploaded_file, "name", "") or ""
    if not name.lower().endswith(".csv"):
        raise CsvImportError(
            "Only .csv files can be imported.",
            details={"file": ["The file must have a .csv extension."]},
        )

    size = getattr(uploaded_file, "size", None)
    if size is None:
        uploaded_file.seek(0, io.SEEK_END)
        size = uploaded_file.tell()
    if size == 0:
        raise CsvImportError("The file is empty.", details={"file": ["The file contains no data."]})
    if size > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        raise CsvImportError(
            f"The file is larger than the {limit_mb:.0f} MB limit.",
            details={"file": [f"Maximum upload size is {limit_mb:.0f} MB."]},
        )

    uploaded_file.seek(0)
    raw = uploaded_file.read()
    if isinstance(raw, str):  # already decoded (e.g. in tests)
        return raw

    # utf-8-sig strips the BOM Excel writes; cp1252 covers Windows exports.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise CsvImportError(
        "The file could not be read as text.",
        details={"file": ["Save the file as UTF-8 encoded CSV and try again."]},
    )


def _detect_dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    """Sniff the delimiter, falling back to a comma."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=SUPPORTED_DELIMITERS)
    except csv.Error:
        return csv.excel


def _map_headers(fieldnames: list[str] | None) -> dict[str, str]:
    """
    Map logical column names to the actual header strings in the file.

    Returns e.g. ``{"name": "Full Name", "phone_number": "Mobile"}``.
    """
    if not fieldnames:
        raise CsvImportError(
            "The file has no header row.",
            details={"file": ["The first row must name the columns, e.g. name,phone_number,opted_in."]},
        )

    normalized = {}
    for header in fieldnames:
        if header is None:
            continue
        key = header.strip().lstrip("﻿").lower().replace(" ", "_").replace("-", "_")
        normalized.setdefault(key, header)

    mapping: dict[str, str] = {}
    for logical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[logical] = normalized[alias]
                break

    missing = [column for column in REQUIRED_COLUMNS if column not in mapping]
    if missing:
        raise CsvImportError(
            f"Missing required column(s): {', '.join(missing)}.",
            details={
                "file": [
                    f"The file must contain a '{column}' column "
                    f"(accepted names: {', '.join(COLUMN_ALIASES[column])})."
                    for column in missing
                ]
            },
        )
    return mapping


def parse_consent(value: str | None) -> bool:
    """
    Interpret a consent cell.

    Only the values in :data:`CONSENT_TRUE_VALUES` grant opt-in. An empty,
    missing, or unrecognised value means "not opted in" — never a guess in the
    permissive direction.
    """
    if value is None:
        return False
    return str(value).strip().lower() in CONSENT_TRUE_VALUES


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------


def parse_rows(text: str, *, default_region: str | None = None, max_rows: int | None = None) -> list[ParsedRow]:
    """
    Parse and normalize every data row.

    In-file duplicates are detected here (the first occurrence wins), so the
    database never sees two rows for the same number in one upload.
    """
    max_rows = max_rows or settings.CSV_IMPORT_MAX_ROWS

    sample = text[:8192]
    reader = csv.DictReader(io.StringIO(text), dialect=_detect_dialect(sample))
    mapping = _map_headers(reader.fieldnames)

    rows: list[ParsedRow] = []
    seen_numbers: dict[str, int] = {}

    for index, raw_row in enumerate(reader, start=1):
        if index > max_rows:
            raise CsvImportError(
                f"The file has more than {max_rows:,} rows.",
                details={"file": [f"Split the file into batches of at most {max_rows:,} rows."]},
            )

        raw = {key: (value or "").strip() for key, value in raw_row.items() if key is not None}
        if not any(raw.values()):
            continue  # skip blank lines entirely rather than reporting them

        row = ParsedRow(row_number=index, raw=raw)

        name = raw.get(mapping["name"], "").strip()
        phone_raw = raw.get(mapping["phone_number"], "").strip()

        if not name:
            row.error = "Name is required."
            rows.append(row)
            continue
        if len(name) > MAX_NAME_LENGTH:
            row.error = f"Name is longer than {MAX_NAME_LENGTH} characters."
            rows.append(row)
            continue
        if not phone_raw:
            row.error = "Phone number is required."
            rows.append(row)
            continue

        try:
            parsed = parse_phone_number(phone_raw, default_region)
        except PhoneNumberError as exc:
            row.error = str(exc)
            rows.append(row)
            continue

        first_seen = seen_numbers.get(parsed.e164)
        if first_seen is not None:
            row.error = f"Duplicate of row {first_seen} in this file ({parsed.e164})."
            row.phone_e164 = parsed.e164
            rows.append(row)
            continue
        seen_numbers[parsed.e164] = index

        email = raw.get(mapping.get("email", ""), "").strip()
        if email and len(email) > MAX_EMAIL_LENGTH:
            email = ""

        row.name = name
        row.phone_e164 = parsed.e164
        row.country_code = parsed.country_code
        row.email = email
        row.opted_in = parse_consent(raw.get(mapping.get("opted_in", "")))
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Import execution
# ---------------------------------------------------------------------------


def run_import(
    contact_import: ContactImport,
    text: str,
    *,
    update_existing: bool = False,
    target_group: ContactGroup | None = None,
    user=None,
    request=None,
    default_region: str | None = None,
) -> ContactImport:
    """
    Execute an import and record its outcome on ``contact_import``.

    Consent rules applied here:

    * A new contact is opted in only when the row carries explicit consent.
    * When ``update_existing`` is set, explicit consent in the file can *grant*
      opt-in to an existing contact, but a missing or negative value never
      silently revokes consent already on record — withdrawal is a deliberate
      act performed through the opt-out action.
    """
    contact_import.status = ImportStatus.PROCESSING
    contact_import.started_at = timezone.now()
    contact_import.target_group = target_group
    contact_import.save(update_fields=["status", "started_at", "target_group", "updated_at"])

    # Parsing happens outside the write transaction: a file-level failure must
    # leave the FAILED status committed, not roll it back with the raise.
    try:
        rows = parse_rows(text, default_region=default_region)
    except CsvImportError as exc:
        contact_import.status = ImportStatus.FAILED
        contact_import.error_message = exc.message
        contact_import.finished_at = timezone.now()
        contact_import.save(
            update_fields=["status", "error_message", "finished_at", "updated_at"]
        )
        raise

    with transaction.atomic():
        return _persist_import(
            contact_import,
            rows,
            update_existing=update_existing,
            target_group=target_group,
            user=user,
            request=request,
        )


def _persist_import(
    contact_import: ContactImport,
    rows: list[ParsedRow],
    *,
    update_existing: bool,
    target_group: ContactGroup | None,
    user,
    request,
) -> ContactImport:
    """Write the parsed rows. Runs inside a single transaction."""
    summary = ImportSummary(total_rows=len(rows))
    report_rows: list[ContactImportRow] = []

    invalid_rows = [row for row in rows if not row.is_valid]
    valid_rows = [row for row in rows if row.is_valid]

    for row in invalid_rows:
        # An in-file duplicate is reported as a duplicate, not as bad data.
        outcome = RowOutcome.DUPLICATE if "Duplicate of row" in row.error else RowOutcome.INVALID
        if outcome == RowOutcome.DUPLICATE:
            summary.duplicates += 1
        else:
            summary.invalid += 1
        report_rows.append(
            ContactImportRow(
                contact_import=contact_import,
                row_number=row.row_number,
                outcome=outcome,
                raw_data=row.raw,
                error_message=row.error[:255],
            )
        )

    # One query resolves every collision with the existing database.
    existing_by_number = {
        contact.phone_number: contact
        for contact in Contact.objects.filter(
            phone_number__in=[row.phone_e164 for row in valid_rows]
        )
    }

    now = timezone.now()
    to_create: list[Contact] = []
    new_rows: list[ParsedRow] = []
    to_update: list[Contact] = []

    for row in valid_rows:
        existing = existing_by_number.get(row.phone_e164)

        if existing is not None and not update_existing:
            summary.duplicates += 1
            report_rows.append(
                ContactImportRow(
                    contact_import=contact_import,
                    row_number=row.row_number,
                    outcome=RowOutcome.DUPLICATE,
                    raw_data=row.raw,
                    error_message=f"{existing.name} already uses {row.phone_e164}.",
                    contact=existing,
                )
            )
            continue

        if existing is not None:
            existing.name = row.name
            if row.email:
                existing.email = row.email
            # Consent may be granted by the file, never revoked by it.
            if row.opted_in and not existing.opted_in:
                existing.opt_in(OptInSource.CSV_IMPORT, when=now)
            if not existing.opted_in:
                summary.not_opted_in += 1
            to_update.append(existing)
            summary.updated += 1
            continue

        contact = Contact(
            name=row.name,
            phone_number=row.phone_e164,
            country_code=row.country_code,
            email=row.email,
            status=ContactStatus.ACTIVE,
            # Derived from the import, never passed separately: a contact that
            # landed in a different organization than the file it came from
            # would be invisible to whoever uploaded it.
            organization_id=contact_import.organization_id,
            created_by=user,
        )
        if row.opted_in:
            contact.opt_in(OptInSource.CSV_IMPORT, when=now)
        else:
            # No consent on the row means no consent recorded. This is the
            # absence of an opt-in, not a withdrawal, so no opt-out source is
            # written — the contact simply stays ineligible until consent
            # arrives through a documented channel.
            summary.not_opted_in += 1

        to_create.append(contact)
        new_rows.append(row)

    if to_create:
        Contact.objects.bulk_create(to_create, batch_size=500)
        summary.imported = len(to_create)

    if to_update:
        Contact.objects.bulk_update(
            to_update,
            [
                "name",
                "email",
                "opted_in",
                "opt_in_source",
                "opt_in_at",
                "opt_out_source",
                "opt_out_at",
                "updated_at",
            ],
            batch_size=500,
        )

    if target_group is not None:
        touched = to_create + to_update
        if touched:
            GroupMembership.objects.bulk_create(
                [
                    GroupMembership(group=target_group, contact=contact, added_by=user)
                    for contact in touched
                ],
                ignore_conflicts=True,
                batch_size=500,
            )

    if report_rows:
        ContactImportRow.objects.bulk_create(report_rows, batch_size=500)

    contact_import.total_rows = summary.total_rows
    contact_import.imported_count = summary.imported
    contact_import.updated_count = summary.updated
    contact_import.duplicate_count = summary.duplicates
    contact_import.invalid_count = summary.invalid
    contact_import.not_opted_in_count = summary.not_opted_in
    contact_import.status = ImportStatus.COMPLETED
    contact_import.finished_at = timezone.now()
    contact_import.save()

    record_audit(
        AuditAction.CONTACTS_IMPORTED,
        user=user,
        request=request,
        obj=contact_import,
        description=f"Imported {summary.imported} contacts from {contact_import.file_name}",
        metadata={
            "total_rows": summary.total_rows,
            "imported": summary.imported,
            "updated": summary.updated,
            "duplicates": summary.duplicates,
            "invalid": summary.invalid,
            "not_opted_in": summary.not_opted_in,
            "group": target_group.name if target_group else None,
        },
    )

    logger.info(
        "CSV import %s finished: %d imported, %d updated, %d duplicates, %d invalid, %d not opted in",
        contact_import.pk,
        summary.imported,
        summary.updated,
        summary.duplicates,
        summary.invalid,
        summary.not_opted_in,
    )
    return contact_import


def import_contacts_from_file(
    uploaded_file: IO,
    *,
    update_existing: bool = False,
    target_group: ContactGroup | None = None,
    organization=None,
    user=None,
    request=None,
) -> ContactImport:
    """Validate an upload, create the import record, and run it."""
    text = validate_upload(uploaded_file)

    contact_import = ContactImport.objects.create(
        file_name=(getattr(uploaded_file, "name", "") or "upload.csv")[:255],
        file_size=getattr(uploaded_file, "size", 0) or len(text.encode("utf-8")),
        organization=organization,
        uploaded_by=user,
    )
    return run_import(
        contact_import,
        text,
        update_existing=update_existing,
        target_group=target_group,
        user=user,
        request=request,
    )
