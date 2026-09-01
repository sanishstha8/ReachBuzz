"""
CSV import.

The consent rules get the most coverage here: an importer that quietly opts
people in is the single most damaging bug this application could ship.
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from contacts.importers import (
    CsvImportError,
    import_contacts_from_file,
    parse_consent,
    parse_rows,
    validate_upload,
)
from contacts.models import Contact, ContactImport, ImportStatus, OptInSource, RowOutcome
from core.models import AuditAction, AuditLog

pytestmark = pytest.mark.django_db


def upload(content: str, name: str = "contacts.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/csv")


HEADER = "name,phone_number,opted_in\n"


class TestFileValidation:
    def test_rejects_a_non_csv_extension(self) -> None:
        with pytest.raises(CsvImportError, match="Only .csv files"):
            validate_upload(upload("a,b", "contacts.txt"))

    def test_rejects_an_empty_file(self) -> None:
        with pytest.raises(CsvImportError, match="empty"):
            validate_upload(SimpleUploadedFile("contacts.csv", b"", content_type="text/csv"))

    def test_rejects_a_file_over_the_size_limit(self) -> None:
        with pytest.raises(CsvImportError, match="larger than"):
            validate_upload(upload("x" * 100), max_bytes=10)

    def test_strips_the_excel_byte_order_mark(self) -> None:
        text = validate_upload(
            SimpleUploadedFile("c.csv", "﻿name,phone_number\n".encode(), content_type="text/csv")
        )
        assert text.startswith("name")

    def test_reads_windows_encoded_files(self) -> None:
        raw = "name,phone_number\nJosé,+9779800000000\n".encode("cp1252")
        text = validate_upload(SimpleUploadedFile("c.csv", raw, content_type="text/csv"))
        assert "José" in text


class TestColumnValidation:
    def test_missing_required_column_is_rejected(self) -> None:
        with pytest.raises(CsvImportError, match="Missing required column"):
            parse_rows("name,email\nAarav,a@example.com\n")

    def test_header_aliases_are_accepted(self) -> None:
        rows = parse_rows("Full Name,Mobile Number\nAarav,+9779800000000\n")
        assert rows[0].name == "Aarav"
        assert rows[0].phone_e164 == "+9779800000000"

    def test_headers_are_case_and_spacing_insensitive(self) -> None:
        rows = parse_rows("  NAME , Phone-Number \nAarav,+9779800000000\n")
        assert rows[0].is_valid

    def test_semicolon_delimited_files_are_handled(self) -> None:
        rows = parse_rows("name;phone_number\nAarav;+9779800000000\n")
        assert rows[0].phone_e164 == "+9779800000000"


class TestConsentParsing:
    @pytest.mark.parametrize(
        "value", ["true", "TRUE", "yes", "Y", "1", "consented", "subscribed", " true "]
    )
    def test_affirmative_values_grant_consent(self, value: str) -> None:
        assert parse_consent(value) is True

    @pytest.mark.parametrize(
        "value", ["false", "no", "0", "", "   ", None, "maybe", "unknown", "pending", "TRUEish"]
    )
    def test_everything_else_is_treated_as_no_consent(self, value) -> None:
        """Ambiguity must never resolve in the permissive direction."""
        assert parse_consent(value) is False


class TestRowParsing:
    def test_normalizes_phone_numbers(self) -> None:
        rows = parse_rows(HEADER + "Aarav,+977 980-000 0000,true\n")
        assert rows[0].phone_e164 == "+9779800000000"

    def test_flags_an_invalid_number(self) -> None:
        rows = parse_rows(HEADER + "Aarav,not-a-number,true\n")
        assert not rows[0].is_valid
        assert "not-a-number" in rows[0].error

    def test_requires_a_name(self) -> None:
        rows = parse_rows(HEADER + ",+9779800000000,true\n")
        assert rows[0].error == "Name is required."

    def test_requires_a_phone_number(self) -> None:
        rows = parse_rows(HEADER + "Aarav,,true\n")
        assert rows[0].error == "Phone number is required."

    def test_detects_in_file_duplicates(self) -> None:
        rows = parse_rows(
            HEADER + "Aarav,+9779800000000,true\n" + "Aarav again,+977 9800000000,true\n"
        )
        assert rows[0].is_valid
        assert "Duplicate of row 1" in rows[1].error

    def test_blank_lines_are_skipped_silently(self) -> None:
        rows = parse_rows(HEADER + "Aarav,+9779800000000,true\n,,\n")
        assert len(rows) == 1

    def test_row_limit_is_enforced(self) -> None:
        body = "".join(f"C{i},+97798{i:08d},true\n" for i in range(5))
        with pytest.raises(CsvImportError, match="more than"):
            parse_rows(HEADER + body, max_rows=3)


class TestImportConsent:
    def test_explicit_true_opts_the_contact_in(self, operator) -> None:
        import_contacts_from_file(upload(HEADER + "Aarav,+9779800000000,true\n"), user=operator)

        contact = Contact.objects.get(phone_number="+9779800000000")
        assert contact.opted_in is True
        assert contact.opt_in_source == OptInSource.CSV_IMPORT
        assert contact.opt_in_at is not None

    def test_explicit_false_imports_opted_out(self, operator) -> None:
        import_contacts_from_file(upload(HEADER + "Sita,+9779811111111,false\n"), user=operator)

        contact = Contact.objects.get(phone_number="+9779811111111")
        assert contact.opted_in is False

    def test_missing_consent_column_never_opts_anyone_in(self, operator) -> None:
        """A spreadsheet of numbers is not permission to message them."""
        result = import_contacts_from_file(
            upload("name,phone_number\nAarav,+9779800000000\nSita,+9779811111111\n"), user=operator
        )

        assert Contact.objects.filter(opted_in=True).count() == 0
        assert result.imported_count == 2
        assert result.not_opted_in_count == 2

    def test_unrecognised_consent_value_does_not_opt_in(self, operator) -> None:
        import_contacts_from_file(upload(HEADER + "Aarav,+9779800000000,maybe\n"), user=operator)
        assert Contact.objects.get(phone_number="+9779800000000").opted_in is False

    def test_contacts_without_consent_are_excluded_from_the_eligible_set(self, operator) -> None:
        import_contacts_from_file(
            upload(HEADER + "Yes,+9779800000000,true\nNo,+9779811111111,false\n"), user=operator
        )
        assert Contact.objects.count() == 2
        assert Contact.objects.eligible().count() == 1


class TestImportOutcomes:
    def test_reports_the_expected_summary(self, operator, make_contact) -> None:
        make_contact("Already here", "+9779822222222")

        result = import_contacts_from_file(
            upload(
                HEADER
                + "Aarav,+9779800000000,true\n"        # imported, opted in
                + "Sita,+9779811111111,false\n"        # imported, not opted in
                + "Existing,+9779822222222,true\n"     # duplicate of the database
                + "Dup,+9779800000000,true\n"          # duplicate within the file
                + "Broken,bad-number,true\n"           # invalid
                + ",+9779833333333,true\n"             # invalid: no name
            ),
            user=operator,
        )

        assert result.total_rows == 6
        assert result.imported_count == 2
        assert result.duplicate_count == 2
        assert result.invalid_count == 2
        assert result.not_opted_in_count == 1
        assert result.status == ImportStatus.COMPLETED

    def test_rejected_rows_are_recorded_with_reasons(self, operator) -> None:
        result = import_contacts_from_file(
            upload(HEADER + "Broken,bad-number,true\n"), user=operator
        )

        row = result.rows.get(outcome=RowOutcome.INVALID)
        assert row.row_number == 1
        assert "bad-number" in row.error_message
        assert row.raw_data["name"] == "Broken"

    def test_duplicate_rows_link_to_the_existing_contact(self, operator, make_contact) -> None:
        existing = make_contact("Existing", "+9779800000000")

        result = import_contacts_from_file(
            upload(HEADER + "Copy,+9779800000000,true\n"), user=operator
        )

        row = result.rows.get(outcome=RowOutcome.DUPLICATE)
        assert row.contact == existing

    def test_existing_contacts_are_not_modified_by_default(self, operator, make_contact) -> None:
        existing = make_contact("Original Name", "+9779800000000", opted_in=False)

        import_contacts_from_file(upload(HEADER + "New Name,+9779800000000,true\n"), user=operator)

        existing.refresh_from_db()
        assert existing.name == "Original Name"
        assert existing.opted_in is False

    def test_update_existing_refreshes_the_contact(self, operator, make_contact) -> None:
        existing = make_contact("Original Name", "+9779800000000")

        result = import_contacts_from_file(
            upload(HEADER + "New Name,+9779800000000,true\n"),
            update_existing=True,
            user=operator,
        )

        existing.refresh_from_db()
        assert existing.name == "New Name"
        assert existing.opted_in is True
        assert result.updated_count == 1
        assert result.imported_count == 0

    def test_update_never_revokes_existing_consent(self, operator, make_contact) -> None:
        """Withdrawal is a deliberate act, not a side effect of a spreadsheet."""
        existing = make_contact("Consenting", "+9779800000000", opted_in=True)

        import_contacts_from_file(
            upload(HEADER + "Consenting,+9779800000000,false\n"),
            update_existing=True,
            user=operator,
        )

        existing.refresh_from_db()
        assert existing.opted_in is True


class TestImportGroups:
    def test_imported_contacts_join_the_target_group(self, operator, group) -> None:
        import_contacts_from_file(
            upload(HEADER + "Aarav,+9779800000000,true\nSita,+9779811111111,true\n"),
            target_group=group,
            user=operator,
        )
        assert group.memberships.count() == 2

    def test_group_is_recorded_on_the_import(self, operator, group) -> None:
        result = import_contacts_from_file(
            upload(HEADER + "Aarav,+9779800000000,true\n"), target_group=group, user=operator
        )
        assert result.target_group == group


class TestImportRecordKeeping:
    def test_import_is_audited_with_its_counts(self, operator) -> None:
        import_contacts_from_file(upload(HEADER + "Aarav,+9779800000000,true\n"), user=operator)

        entry = AuditLog.objects.get(action=AuditAction.CONTACTS_IMPORTED)
        assert entry.user == operator
        assert entry.metadata["imported"] == 1

    def test_file_metadata_is_stored(self, operator) -> None:
        result = import_contacts_from_file(
            upload(HEADER + "Aarav,+9779800000000,true\n", name="june-list.csv"), user=operator
        )
        assert result.file_name == "june-list.csv"
        assert result.file_size > 0
        assert result.started_at is not None
        assert result.finished_at is not None

    def test_a_failed_import_keeps_its_failed_status(self, operator) -> None:
        """The FAILED status must survive the exception, not roll back with it."""
        with pytest.raises(CsvImportError):
            import_contacts_from_file(upload("wrong,headers\na,b\n"), user=operator)

        record = ContactImport.objects.get()
        assert record.status == ImportStatus.FAILED
        assert record.error_message

    def test_nothing_is_written_when_the_file_is_unusable(self, operator) -> None:
        with pytest.raises(CsvImportError):
            import_contacts_from_file(upload("wrong,headers\na,b\n"), user=operator)
        assert Contact.objects.count() == 0


class TestImportScale:
    def test_five_hundred_rows_import_in_a_bounded_number_of_queries(
        self, operator, django_assert_max_num_queries
    ) -> None:
        """Row-at-a-time queries would make a 1,000-contact import unusable."""
        body = "".join(f"Contact {i},+97798{i:08d},true\n" for i in range(500))

        # A fixed query budget regardless of row count: the importer batches.
        with django_assert_max_num_queries(15):
            result = import_contacts_from_file(upload(HEADER + body), user=operator)

        assert result.imported_count == 500
        assert Contact.objects.eligible().count() == 500

    def test_query_count_does_not_grow_with_row_count(
        self, operator, django_assert_max_num_queries
    ) -> None:
        """Guards against a regression to per-row queries."""
        body = "".join(f"Contact {i},+97798{i:08d},true\n" for i in range(1000))

        with django_assert_max_num_queries(15):
            result = import_contacts_from_file(upload(HEADER + body), user=operator)

        assert result.imported_count == 1000
