"""
Step three of the retrofit: the tenant becomes required.

Split from the column's creation because a non-null foreign key cannot be
added to a table that already holds rows. Those rows were given an owner by
``organizations.0002``, which this depends on explicitly — the graph must never
be free to tighten the column before it has been filled.

From here a row without an organization cannot be saved. That matters more
than it sounds: an unowned row is invisible to every scoped query, so the bug
it causes is silent data loss rather than an error anybody would notice.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0002_backfill_default_organization"),
        ("whatsapp", "0003_messagetemplate_organization"),
    ]

    operations = [
        migrations.AlterField(
            model_name="messagetemplate",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="organizations.organization",
            ),
        ),
    ]
