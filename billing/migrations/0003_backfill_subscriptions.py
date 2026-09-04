"""
Give every organization that already exists a subscription.

**Existing organizations land on Self-hosted, which has no limits.** This is the
one decision in the stage that could break a working system, so it is made in
the safe direction: a business that has been running this software must not
discover one morning that it has been retroactively placed on a tier it never
chose, with a ceiling it never agreed to, halfway through a campaign. They are
running it on their own infrastructure against their own WABA, which is what
Self-hosted describes. Moving them somewhere else is a commercial conversation,
and a commercial conversation is not something a migration gets to have.

New signups are a different case entirely — they choose a plan by signing up for
it, and ``billing.services.subscribe()`` starts them on the cheapest public one.

Same shape as the Stage 1 organization backfill: idempotent going forward,
detaching rather than destroying going back.
"""

from django.db import migrations
from django.utils import timezone

#: Matches billing.models.add_months. Duplicated rather than imported because a
#: migration must keep working when the module it came from has moved on.
def _add_months(moment, months):
    import calendar

    index = moment.month - 1 + months
    year = moment.year + index // 12
    month = index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def backfill(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    Plan = apps.get_model("billing", "Plan")
    Subscription = apps.get_model("billing", "Subscription")

    plan = Plan.objects.filter(slug="self-hosted").first()
    if plan is None:  # The catalogue was edited away; nothing sensible to do.
        return

    now = timezone.now()
    for organization in Organization.objects.filter(subscription__isnull=True):
        Subscription.objects.create(
            organization=organization,
            plan=plan,
            status="active",
            current_period_start=now,
            current_period_end=_add_months(now, 1),
            trial_end=None,
        )


def unbackfill(apps, schema_editor):
    """
    Remove only the subscriptions this migration would have created.

    Anything on a different plan was put there deliberately, by a signup or by
    an operator, and reversing a schema change is not a reason to undo that.
    """
    Subscription = apps.get_model("billing", "Subscription")
    Subscription.objects.filter(plan__slug="self-hosted", status="active").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0002_seed_plans"),
        ("organizations", "0002_backfill_default_organization"),
    ]

    operations = [migrations.RunPython(backfill, unbackfill)]
