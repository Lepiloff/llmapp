# Generated manually for Phase 4 re-actualization metadata.
from __future__ import annotations

from django.db import migrations, models
from django.db.models import F


def backfill_last_enriched_at(apps, schema_editor):
    Source = apps.get_model("sources", "Source")
    Source.objects.filter(last_enriched_at__isnull=True).update(
        last_enriched_at=F("fetched_at")
    )


class Migration(migrations.Migration):

    dependencies = [
        ("sources", "0004_alter_source_source_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="source",
            name="last_enriched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_last_enriched_at,
            migrations.RunPython.noop,
        ),
    ]
