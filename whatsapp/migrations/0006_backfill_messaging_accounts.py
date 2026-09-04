"""
Give every existing organization the sender it has been using.

Before Stage 5 there was one WhatsApp Business Account for the installation,
configured in the environment. Those credentials keep working — the provider
still falls back to them — but an organization with a messaging account of its
own is the state everything after this stage expects, so the organizations that
already exist are given one built from what they were already using.

**Nothing is invented.** If the environment has no ``META_PHONE_NUMBER_ID``,
this does nothing at all: an account with no number could not send, would claim
the empty string in a unique column, and would be a worse starting point than
the environment fallback it replaced. A mock-provider development database is
exactly that case, and it is left alone.

The access token is copied *encrypted*, using the same code path the
application uses, so a token written by this migration reads back the same way
as one entered by hand.

Reverses by deleting only the accounts it would have created — one matching the
environment's own phone number id. An account somebody connected themselves is
left alone; undoing a schema change is not a reason to disconnect a customer's
sender.
"""

from django.conf import settings
from django.db import migrations


def backfill(apps, schema_editor):
    from core.encryption import encrypt

    Organization = apps.get_model("organizations", "Organization")
    MessagingAccount = apps.get_model("whatsapp", "MessagingAccount")

    provider = (getattr(settings, "WHATSAPP_PROVIDER", "mock") or "mock").strip().lower()
    phone_number_id = getattr(settings, "META_PHONE_NUMBER_ID", "")
    access_token = getattr(settings, "META_ACCESS_TOKEN", "")

    # Nothing to copy. A development database on the mock provider lands here,
    # and the environment fallback keeps it working exactly as before.
    if provider != "meta" or not phone_number_id:
        return

    # The number is unique across the table, so only one organization can hold
    # it. The oldest is the one that has actually been sending with it.
    organization = Organization.objects.order_by("created_at").first()
    if organization is None:
        return

    if MessagingAccount.objects.filter(phone_number_id=phone_number_id).exists():
        return

    MessagingAccount.objects.create(
        organization=organization,
        provider="meta",
        label="Migrated from the environment",
        phone_number_id=phone_number_id,
        waba_id=getattr(settings, "META_WABA_ID", ""),
        access_token_encrypted=encrypt(access_token),
        # Not "active": nothing has checked these credentials still work. The
        # verify command sets that, and until it does the environment fallback
        # is what sends, which is what was sending yesterday.
        status="unverified",
        is_default=True,
    )


def unbackfill(apps, schema_editor):
    MessagingAccount = apps.get_model("whatsapp", "MessagingAccount")

    phone_number_id = getattr(settings, "META_PHONE_NUMBER_ID", "")
    if not phone_number_id:
        return

    MessagingAccount.objects.filter(
        phone_number_id=phone_number_id, label="Migrated from the environment"
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("whatsapp", "0005_messagingaccount"),
        ("organizations", "0002_backfill_default_organization"),
    ]

    operations = [migrations.RunPython(backfill, unbackfill)]
