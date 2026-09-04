"""
The starting plan catalogue.

The three tiers already advertised on the pricing page, turned into rows. They
were hard-coded strings in ``pages/views.py`` until now, promising limits that
nothing enforced; this migration is what makes them real, and the page reads
from here afterwards so the two cannot drift apart.

**Contact limits come from what the page already said. Message and team limits
are left unlimited.** "Up to 1,000 contacts" is a commitment this project has
been making in public, so enforcing exactly that number is honest. A monthly
message cap was never advertised, and inventing one here would be deciding a
commercial question in a migration — the same reason ``price`` stays empty and
renders as "Pricing on request". The enforcement is built and tested; setting a
figure is a number in the admin, not a code change.
"""

from django.db import migrations

SEED = [
    {
        "slug": "starter",
        "name": "Starter",
        "summary": "For a single team sending occasional campaigns.",
        "max_contacts": 1000,
        "max_messages_per_month": None,
        "max_team_members": None,
        "trial_days": 14,
        "featured": False,
        "sort_order": 10,
        "features": [
            "Up to 1,000 contacts",
            "CSV import and contact groups",
            "Approved template messaging",
            "Delivery and read reporting",
            "Email support",
        ],
    },
    {
        "slug": "business",
        "name": "Business",
        "summary": "For regular campaigns to a growing audience.",
        "max_contacts": 10000,
        "max_messages_per_month": None,
        "max_team_members": None,
        "trial_days": 14,
        "featured": True,
        "sort_order": 20,
        "features": [
            "Up to 10,000 contacts",
            "Everything in Starter",
            "Scheduled campaigns",
            "CSV exports and the reporting API",
            "Role-based access for your team",
        ],
    },
    {
        "slug": "self-hosted",
        "name": "Self-hosted",
        "summary": "Run it on your own infrastructure, against your own WABA.",
        "max_contacts": None,
        "max_messages_per_month": None,
        "max_team_members": None,
        "trial_days": 0,
        "featured": False,
        "sort_order": 30,
        "features": [
            "No contact limit",
            "Your database, your Redis, your logs",
            "Full REST API and OpenAPI schema",
            "Connect your own Meta Business account",
            "Deployment guidance",
        ],
    },
]


def seed_plans(apps, schema_editor):
    """
    Idempotent, so a restored snapshot can be migrated forward safely.

    ``get_or_create`` rather than ``create``: an operator who has already edited
    a plan's limits must not have that overwritten by re-running a migration.
    """
    Plan = apps.get_model("billing", "Plan")

    for row in SEED:
        Plan.objects.get_or_create(
            slug=row["slug"],
            defaults={
                # price stays NULL: see the module docstring.
                "price": None,
                "currency": "USD",
                "interval": "monthly",
                "is_active": True,
                "is_public": True,
                **row,
            },
        )


def unseed_plans(apps, schema_editor):
    """
    Remove only the untouched seeds.

    A plan somebody is subscribed to is left alone — the foreign key is PROTECT
    precisely so that deleting it cannot silently erase what a customer bought,
    and a reversal is not a good enough reason to override that.
    """
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(slug__in=[row["slug"] for row in SEED], subscriptions__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("billing", "0001_initial")]

    operations = [migrations.RunPython(seed_plans, unseed_plans)]
