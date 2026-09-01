"""REST API for contacts, groups and CSV import."""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from contacts.models import Contact, ContactGroup, ContactStatus, GroupMembership

pytestmark = pytest.mark.django_db


def upload(content: str, name: str = "contacts.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/csv")


HEADER = "name,phone_number,opted_in\n"

LIST_URL = reverse("contacts-api:contact-list")


class TestContactListEndpoint:
    def test_requires_authentication(self, api_client: APIClient) -> None:
        assert api_client.get(LIST_URL).status_code in (401, 403)

    def test_returns_paginated_contacts(self, auth_api_client: APIClient, make_contact) -> None:
        for i in range(3):
            make_contact(f"Contact {i}")

        response = auth_api_client.get(LIST_URL)

        assert response.status_code == 200
        assert response.data["count"] == 3
        assert "num_pages" in response.data

    def test_search_filters_results(self, auth_api_client: APIClient, make_contact) -> None:
        make_contact("Aarav Sharma", "+9779800000000")
        make_contact("Sita Rai", "+9779811111111")

        response = auth_api_client.get(LIST_URL, {"search": "aarav"})

        assert response.data["count"] == 1

    def test_eligible_filter_excludes_non_consenting_contacts(
        self, auth_api_client: APIClient, make_contact
    ) -> None:
        make_contact("Yes", opted_in=True)
        make_contact("No", opted_in=False)
        make_contact("Inactive", opted_in=True, status=ContactStatus.INACTIVE)

        response = auth_api_client.get(LIST_URL, {"eligible": "true"})

        assert response.data["count"] == 1

    def test_group_filter(self, auth_api_client: APIClient, group_with_members) -> None:
        response = auth_api_client.get(LIST_URL, {"group": str(group_with_members.pk)})
        assert response.data["count"] == 3


class TestContactCreateEndpoint:
    def test_creates_a_contact_with_a_normalized_number(self, auth_api_client: APIClient) -> None:
        response = auth_api_client.post(
            LIST_URL, {"name": "Aarav", "phone_number": "+977 980-000 0000"}, format="json"
        )

        assert response.status_code == 201
        assert response.data["phone_number"] == "+9779800000000"

    def test_omitting_consent_creates_an_opted_out_contact(self, auth_api_client: APIClient) -> None:
        response = auth_api_client.post(
            LIST_URL, {"name": "Aarav", "phone_number": "+9779800000000"}, format="json"
        )
        assert response.data["opted_in"] is False

    def test_explicit_consent_is_honoured(self, auth_api_client: APIClient) -> None:
        response = auth_api_client.post(
            LIST_URL,
            {"name": "Aarav", "phone_number": "+9779800000000", "opted_in": True},
            format="json",
        )
        assert response.data["opted_in"] is True
        assert response.data["opt_in_source"] == "manual"

    def test_invalid_number_returns_400_with_a_field_error(
        self, auth_api_client: APIClient
    ) -> None:
        response = auth_api_client.post(
            LIST_URL, {"name": "Bad", "phone_number": "nonsense"}, format="json"
        )

        assert response.status_code == 400
        assert "phone_number" in response.data["errors"]

    def test_duplicate_number_returns_409(self, auth_api_client: APIClient, make_contact) -> None:
        make_contact("Existing", "+9779800000000")

        response = auth_api_client.post(
            LIST_URL, {"name": "Copy", "phone_number": "+9779800000000"}, format="json"
        )

        assert response.status_code == 409
        assert response.data["code"] == "conflict"


class TestContactDetailEndpoint:
    def test_update_changes_the_name(self, auth_api_client: APIClient, make_contact) -> None:
        contact = make_contact("Old")
        url = reverse("contacts-api:contact-detail", args=[contact.pk])

        response = auth_api_client.patch(url, {"name": "New"}, format="json")

        assert response.status_code == 200
        contact.refresh_from_db()
        assert contact.name == "New"

    def test_consent_cannot_be_changed_by_update(
        self, auth_api_client: APIClient, opted_out_contact
    ) -> None:
        url = reverse("contacts-api:contact-detail", args=[opted_out_contact.pk])

        auth_api_client.patch(url, {"opted_in": True}, format="json")

        opted_out_contact.refresh_from_db()
        assert opted_out_contact.opted_in is False

    def test_delete_removes_the_contact(self, auth_api_client: APIClient, make_contact) -> None:
        contact = make_contact()
        url = reverse("contacts-api:contact-detail", args=[contact.pk])

        assert auth_api_client.delete(url).status_code == 204
        assert Contact.objects.count() == 0


class TestConsentActions:
    def test_opt_in_action(self, auth_api_client: APIClient, opted_out_contact) -> None:
        url = reverse("contacts-api:contact-opt-in", args=[opted_out_contact.pk])

        response = auth_api_client.post(url, {"source": "web_form"}, format="json")

        assert response.status_code == 200
        assert response.data["opted_in"] is True
        assert response.data["opt_in_source"] == "web_form"

    def test_opt_out_action(self, auth_api_client: APIClient, opted_in_contact) -> None:
        url = reverse("contacts-api:contact-opt-out", args=[opted_in_contact.pk])

        response = auth_api_client.post(url, {}, format="json")

        assert response.status_code == 200
        assert response.data["opted_in"] is False
        assert response.data["is_eligible"] is False


class TestGroupEndpoints:
    def test_create_group(self, auth_api_client: APIClient) -> None:
        response = auth_api_client.post(
            reverse("contacts-api:contactgroup-list"), {"name": "VIPs"}, format="json"
        )
        assert response.status_code == 201
        assert ContactGroup.objects.filter(name="VIPs").exists()

    def test_list_reports_member_and_eligible_counts(
        self, auth_api_client: APIClient, group_with_members
    ) -> None:
        response = auth_api_client.get(reverse("contacts-api:contactgroup-list"))

        group = response.data["results"][0]
        assert group["member_count"] == 3
        assert group["eligible_count"] == 2

    def test_add_members_is_idempotent(
        self, auth_api_client: APIClient, group, make_contact
    ) -> None:
        contact = make_contact()
        url = reverse("contacts-api:contactgroup-add-members", args=[group.pk])

        auth_api_client.post(url, {"contact_ids": [str(contact.pk)]}, format="json")
        response = auth_api_client.post(url, {"contact_ids": [str(contact.pk)]}, format="json")

        assert response.status_code == 200
        assert GroupMembership.objects.filter(group=group).count() == 1

    def test_remove_members(self, auth_api_client: APIClient, group_with_members) -> None:
        contact = group_with_members.contacts.first()
        url = reverse("contacts-api:contactgroup-remove-members", args=[group_with_members.pk])

        response = auth_api_client.post(url, {"contact_ids": [str(contact.pk)]}, format="json")

        assert response.status_code == 200
        assert group_with_members.memberships.count() == 2

    def test_members_listing(self, auth_api_client: APIClient, group_with_members) -> None:
        url = reverse("contacts-api:contactgroup-members", args=[group_with_members.pk])
        response = auth_api_client.get(url)

        assert response.status_code == 200
        assert response.data["count"] == 3


class TestImportEndpoint:
    def test_uploads_and_returns_the_report(self, auth_api_client: APIClient) -> None:
        response = auth_api_client.post(
            reverse("contacts-api:contact-import-create"),
            {"file": upload(HEADER + "Aarav,+9779800000000,true\nBad,nope,true\n")},
            format="multipart",
        )

        assert response.status_code == 201
        assert response.data["imported_count"] == 1
        assert response.data["invalid_count"] == 1
        assert len(response.data["rows"]) == 1

    def test_rejects_a_non_csv_file(self, auth_api_client: APIClient) -> None:
        response = auth_api_client.post(
            reverse("contacts-api:contact-import-create"),
            {"file": upload("name,phone_number\n", "contacts.txt")},
            format="multipart",
        )

        assert response.status_code == 400
        assert response.data["code"] == "csv_import_failed"

    def test_import_history_is_listed(self, auth_api_client: APIClient) -> None:
        auth_api_client.post(
            reverse("contacts-api:contact-import-create"),
            {"file": upload(HEADER + "Aarav,+9779800000000,true\n")},
            format="multipart",
        )

        response = auth_api_client.get(reverse("contacts-api:contactimport-list"))

        assert response.status_code == 200
        assert response.data["count"] == 1


class TestStatsEndpoint:
    def test_reports_aggregate_counts(
        self, auth_api_client: APIClient, make_contact, group
    ) -> None:
        make_contact("Yes", opted_in=True)
        make_contact("No", opted_in=False)
        make_contact("Inactive", opted_in=True, status=ContactStatus.INACTIVE)

        response = auth_api_client.get(reverse("contacts-api:contact-stats"))

        assert response.data["total"] == 3
        assert response.data["opted_in"] == 2
        assert response.data["opted_out"] == 1
        assert response.data["eligible"] == 1
        assert response.data["groups"] == 1


class TestAuthorization:
    """A Viewer may read the audience but never change who is on it."""

    def _viewer_client(self, viewer) -> APIClient:
        client = APIClient()
        client.force_login(viewer)
        return client

    def test_viewer_can_read_contacts(self, viewer, make_contact) -> None:
        make_contact()
        assert self._viewer_client(viewer).get(LIST_URL).status_code == 200

    def test_viewer_cannot_create_a_contact(self, viewer) -> None:
        response = self._viewer_client(viewer).post(
            LIST_URL, {"name": "Aarav", "phone_number": "+9779800000000"}, format="json"
        )
        assert response.status_code == 403

    def test_viewer_cannot_delete_a_contact(self, viewer, make_contact) -> None:
        contact = make_contact()
        url = reverse("contacts-api:contact-detail", args=[contact.pk])

        assert self._viewer_client(viewer).delete(url).status_code == 403
        assert Contact.objects.count() == 1

    def test_viewer_cannot_change_consent(self, viewer, opted_out_contact) -> None:
        url = reverse("contacts-api:contact-opt-in", args=[opted_out_contact.pk])

        assert self._viewer_client(viewer).post(url, {}, format="json").status_code == 403
        opted_out_contact.refresh_from_db()
        assert opted_out_contact.opted_in is False

    def test_viewer_cannot_import_contacts(self, viewer) -> None:
        response = self._viewer_client(viewer).post(
            reverse("contacts-api:contact-import-create"),
            {"file": upload(HEADER + "Aarav,+9779800000000,true\n")},
            format="multipart",
        )

        assert response.status_code == 403
        assert Contact.objects.count() == 0

    def test_deactivated_operator_loses_access(self, operator) -> None:
        client = APIClient()
        client.force_login(operator)
        operator.is_active = False
        operator.save(update_fields=["is_active"])

        assert client.get(LIST_URL).status_code in (401, 403)
