"""
Give every existing row an organization.

Step two of the three-step retrofit: the columns were added nullable, this
fills them, and the next migration makes them required. Splitting it this way
is what lets the change run against a database that already holds data — a
single migration adding a non-null foreign key to a populated table cannot.

The organization created here is a real one, not a placeholder. On a system
that has been in use, everything already in it genuinely does belong to the
one business that has been operating it; this names that business rather than
inventing a new owner for its data.

On a fresh install there are no users and nothing to own, so it does nothing.
"""

from django.db import migrations
from django.utils.text import slugify

DEFAULT_NAME = "Default Organization"

# Models that gained an organization column in step one.
OWNED = [
    ("contacts", "Contact"),
    ("contacts", "ContactGroup"),
    ("contacts", "ContactImport"),
    ("campaigns", "Campaign"),
    ("whatsapp", "MessageTemplate"),
    ("messaging", "Message"),
]

# The platform roles this project already had, mapped onto seats in the
# organization. Everyone keeps the authority they had.
ROLE_MAP = {"admin": "admin", "operator": "member", "viewer": "member"}


def backfill(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Organization = apps.get_model("organizations", "Organization")
    OrganizationMember = apps.get_model("organizations", "OrganizationMember")

    # An owner is required, so with no users there is nothing to create and,
    # by definition, no data needing an owner either.
    owner = User.objects.order_by("-is_superuser", "date_joined").first()
    if owner is None:
        return

    organization, _created = Organization.objects.get_or_create(
        slug=slugify(DEFAULT_NAME),
        defaults={"name": DEFAULT_NAME, "owner": owner, "status": "active"},
    )

    for user in User.objects.all():
        OrganizationMember.objects.get_or_create(
            organization=organization,
            user=user,
            defaults={"role": "owner" if user.pk == owner.pk else ROLE_MAP.get(user.role, "member")},
        )

    for app_label, model_name in OWNED:
        model = apps.get_model(app_label, model_name)
        model.objects.filter(organization__isnull=True).update(organization=organization)


def unbackfill(apps, schema_editor):
    """
    Detach the rows again, but leave the organization itself.

    Deleting it would cascade to every contact, campaign and message it was
    given — reversing a migration must not destroy the data it was reversing
    the ownership of.
    """
    for app_label, model_name in OWNED:
        model = apps.get_model(app_label, model_name)
        model.objects.update(organization=None)


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0001_initial"),
        # Every app whose column this fills must already have it.
        ("contacts", "0002_contact_organization_contactgroup_organization_and_more"),
        ("campaigns", "0002_campaign_organization"),
        ("whatsapp", "0003_messagetemplate_organization"),
        ("messaging", "0003_message_organization"),
    ]

    operations = [migrations.RunPython(backfill, unbackfill)]
