"""HTML pages for contacts, groups and CSV import."""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from contacts.models import Contact, ContactGroup, ContactStatus

pytestmark = pytest.mark.django_db

HEADER = "name,phone_number,opted_in\n"


def upload(content: str, name: str = "contacts.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/csv")


def viewer_client(viewer) -> Client:
    client = Client()
    client.force_login(viewer)
    return client


class TestContactListPage:
    def test_requires_authentication(self, client: Client) -> None:
        response = client.get(reverse("contacts:list"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_renders_contacts(self, auth_client: Client, make_contact) -> None:
        make_contact("Aarav Sharma", "+9779800000000")
        response = auth_client.get(reverse("contacts:list"))

        assert response.status_code == 200
        assert "Aarav Sharma" in response.content.decode()

    def test_empty_state_when_there_are_no_contacts(self, auth_client: Client) -> None:
        response = auth_client.get(reverse("contacts:list"))
        assert "No contacts yet" in response.content.decode()

    def test_search_filters_the_table(self, auth_client: Client, make_contact) -> None:
        make_contact("Aarav Sharma", "+9779800000000")
        make_contact("Sita Rai", "+9779811111111")

        body = auth_client.get(reverse("contacts:list"), {"search": "sita"}).content.decode()

        assert "Sita Rai" in body
        assert "Aarav Sharma" not in body

    def test_pagination_preserves_filters(self, auth_client: Client, make_contact) -> None:
        for i in range(30):
            make_contact(f"Person {i:02d}")

        response = auth_client.get(reverse("contacts:list"), {"search": "Person", "page": 2})

        assert response.status_code == 200
        assert response.context["page_obj"].number == 2
        assert "search=Person" in response.context["querystring"]

    def test_viewer_sees_no_management_buttons(self, viewer, make_contact) -> None:
        make_contact()
        body = viewer_client(viewer).get(reverse("contacts:list")).content.decode()

        assert "Add contact" not in body
        assert "Import CSV" not in body


class TestContactCreatePage:
    def test_operator_can_create_a_contact(self, auth_client: Client) -> None:
        response = auth_client.post(
            reverse("contacts:create"),
            {
                "name": "Aarav",
                "phone_number": "+977 980 0000000",
                "email": "",
                "status": ContactStatus.ACTIVE,
                "notes": "",
            },
        )

        assert response.status_code == 302
        assert Contact.objects.get().phone_number == "+9779800000000"

    def test_created_without_consent_by_default(self, auth_client: Client) -> None:
        auth_client.post(
            reverse("contacts:create"),
            {"name": "Aarav", "phone_number": "+9779800000000", "status": ContactStatus.ACTIVE},
        )
        assert Contact.objects.get().opted_in is False

    def test_consent_checkbox_records_opt_in(self, auth_client: Client) -> None:
        auth_client.post(
            reverse("contacts:create"),
            {
                "name": "Aarav",
                "phone_number": "+9779800000000",
                "status": ContactStatus.ACTIVE,
                "opted_in": "on",
            },
        )
        contact = Contact.objects.get()
        assert contact.opted_in is True
        assert contact.opt_in_source == "manual"

    def test_invalid_number_re_renders_with_an_error(self, auth_client: Client) -> None:
        response = auth_client.post(
            reverse("contacts:create"),
            {"name": "Bad", "phone_number": "nonsense", "status": ContactStatus.ACTIVE},
        )

        assert response.status_code == 200
        assert Contact.objects.count() == 0
        assert "phone_number" in response.context["form"].errors

    def test_duplicate_number_is_reported_on_the_field(
        self, auth_client: Client, make_contact
    ) -> None:
        make_contact("Existing", "+9779800000000")

        response = auth_client.post(
            reverse("contacts:create"),
            {"name": "Copy", "phone_number": "+9779800000000", "status": ContactStatus.ACTIVE},
        )

        assert "already uses" in str(response.context["form"].errors["phone_number"])

    def test_viewer_is_denied(self, viewer) -> None:
        response = viewer_client(viewer).get(reverse("contacts:create"))
        assert response.status_code == 302
        assert Contact.objects.count() == 0


class TestContactDetailPage:
    def test_renders_consent_state(self, auth_client: Client, opted_in_contact) -> None:
        body = auth_client.get(
            reverse("contacts:detail", args=[opted_in_contact.pk])
        ).content.decode()

        assert "Opted in" in body
        assert opted_in_contact.phone_number in body

    def test_shows_why_a_contact_cannot_be_messaged(
        self, auth_client: Client, opted_out_contact
    ) -> None:
        body = auth_client.get(
            reverse("contacts:detail", args=[opted_out_contact.pk])
        ).content.decode()

        assert "excluded from every campaign" in body


class TestConsentToggle:
    def test_opt_in_requires_post(self, auth_client: Client, opted_out_contact) -> None:
        """A GET consent change could be triggered by a prefetch or an image."""
        url = reverse("contacts:consent", args=[opted_out_contact.pk, "opt-in"])
        assert auth_client.get(url).status_code == 405

    def test_opt_in_records_consent(self, auth_client: Client, opted_out_contact) -> None:
        url = reverse("contacts:consent", args=[opted_out_contact.pk, "opt-in"])

        response = auth_client.post(url)

        assert response.status_code == 302
        opted_out_contact.refresh_from_db()
        assert opted_out_contact.opted_in is True

    def test_opt_out_withdraws_consent(self, auth_client: Client, opted_in_contact) -> None:
        url = reverse("contacts:consent", args=[opted_in_contact.pk, "opt-out"])

        auth_client.post(url)

        opted_in_contact.refresh_from_db()
        assert opted_in_contact.opted_in is False

    def test_csrf_token_is_required(self, operator, opted_out_contact) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(operator)
        url = reverse("contacts:consent", args=[opted_out_contact.pk, "opt-in"])

        assert csrf_client.post(url).status_code == 403
        opted_out_contact.refresh_from_db()
        assert opted_out_contact.opted_in is False

    def test_viewer_cannot_change_consent(self, viewer, opted_out_contact) -> None:
        url = reverse("contacts:consent", args=[opted_out_contact.pk, "opt-in"])

        response = viewer_client(viewer).post(url)

        assert response.status_code == 302
        opted_out_contact.refresh_from_db()
        assert opted_out_contact.opted_in is False


class TestContactDelete:
    def test_operator_can_delete(self, auth_client: Client, make_contact) -> None:
        contact = make_contact()
        response = auth_client.post(reverse("contacts:delete", args=[contact.pk]))

        assert response.status_code == 302
        assert Contact.objects.count() == 0

    def test_viewer_is_denied(self, viewer, make_contact) -> None:
        contact = make_contact()
        viewer_client(viewer).post(reverse("contacts:delete", args=[contact.pk]))
        assert Contact.objects.count() == 1


class TestGroupPages:
    def test_group_list_renders_counts(self, auth_client: Client, group_with_members) -> None:
        body = auth_client.get(reverse("contacts:group-list")).content.decode()
        assert "Newsletter" in body

    def test_create_group(self, auth_client: Client) -> None:
        response = auth_client.post(
            reverse("contacts:group-create"), {"name": "VIPs", "description": "Best customers"}
        )
        assert response.status_code == 302
        assert ContactGroup.objects.filter(name="VIPs").exists()

    def test_group_detail_lists_members(self, auth_client: Client, group_with_members) -> None:
        response = auth_client.get(reverse("contacts:group-detail", args=[group_with_members.pk]))

        assert response.status_code == 200
        assert response.context["member_count"] == 3
        assert response.context["eligible_count"] == 2

    def test_warns_when_no_member_can_be_messaged(
        self, auth_client: Client, group, make_contact
    ) -> None:
        from contacts.models import GroupMembership

        GroupMembership.objects.create(group=group, contact=make_contact(opted_in=False))

        body = auth_client.get(reverse("contacts:group-detail", args=[group.pk])).content.decode()

        assert "would have no recipients" in body

    def test_removing_members_keeps_the_contacts(
        self, auth_client: Client, group_with_members
    ) -> None:
        contact = group_with_members.contacts.first()

        auth_client.post(
            reverse("contacts:group-members", args=[group_with_members.pk]),
            {"action": "remove", "contact_ids": [str(contact.pk)]},
        )

        assert group_with_members.memberships.count() == 2
        assert Contact.objects.count() == 3

    def test_deleting_a_group_keeps_the_contacts(
        self, auth_client: Client, group_with_members
    ) -> None:
        auth_client.post(reverse("contacts:group-delete", args=[group_with_members.pk]))

        assert ContactGroup.objects.count() == 0
        assert Contact.objects.count() == 3

    def test_viewer_cannot_create_a_group(self, viewer) -> None:
        viewer_client(viewer).post(reverse("contacts:group-create"), {"name": "Sneaky"})
        assert ContactGroup.objects.count() == 0


class TestImportPage:
    def test_upload_redirects_to_the_report(self, auth_client: Client) -> None:
        response = auth_client.post(
            reverse("contacts:import"),
            {"file": upload(HEADER + "Aarav,+9779800000000,true\n"), "confirm_consent": "on"},
        )

        assert response.status_code == 302
        assert Contact.objects.count() == 1

    def test_consent_confirmation_is_mandatory(self, auth_client: Client) -> None:
        response = auth_client.post(
            reverse("contacts:import"),
            {"file": upload(HEADER + "Aarav,+9779800000000,true\n")},
        )

        assert response.status_code == 200
        assert "confirm_consent" in response.context["form"].errors
        assert Contact.objects.count() == 0

    def test_bad_file_re_renders_with_an_error(self, auth_client: Client) -> None:
        response = auth_client.post(
            reverse("contacts:import"),
            {"file": upload("wrong,headers\na,b\n"), "confirm_consent": "on"},
        )

        assert response.status_code == 200
        assert "file" in response.context["form"].errors
        assert Contact.objects.count() == 0

    def test_report_shows_the_summary(self, auth_client: Client) -> None:
        auth_client.post(
            reverse("contacts:import"),
            {
                "file": upload(
                    HEADER + "Aarav,+9779800000000,true\nSita,+9779811111111,\nBad,nope,true\n"
                ),
                "confirm_consent": "on",
            },
        )

        from contacts.models import ContactImport

        report = ContactImport.objects.get()
        body = auth_client.get(reverse("contacts:import-detail", args=[report.pk])).content.decode()

        assert "Import summary" in body
        assert "Not opted in" in body
        assert "were imported" in body or "was imported" in body

    def test_sample_csv_is_downloadable(self, auth_client: Client) -> None:
        response = auth_client.get(reverse("contacts:import-sample"))

        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"name,phone_number" in response.content

    def test_viewer_cannot_reach_the_import_page(self, viewer) -> None:
        response = viewer_client(viewer).get(reverse("contacts:import"))
        assert response.status_code == 302


class TestNavigation:
    def test_sidebar_links_to_contacts_and_groups(self, auth_client: Client) -> None:
        body = auth_client.get(reverse("dashboard:home")).content.decode()

        assert reverse("contacts:list") in body
        assert reverse("contacts:group-list") in body
