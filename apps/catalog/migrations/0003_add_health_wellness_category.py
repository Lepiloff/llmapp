from __future__ import annotations

from django.db import migrations


def add_health_wellness_category(apps, schema_editor) -> None:
    Category = apps.get_model("catalog", "Category")
    Category.objects.get_or_create(
        slug="health-wellness",
        defaults={
            "name": "Health & Wellness",
            "sort_order": 110,
        },
    )


def remove_health_wellness_category(apps, schema_editor) -> None:
    Category = apps.get_model("catalog", "Category")
    Category.objects.filter(slug="health-wellness").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0002_expand_capability_note_to_500"),
    ]

    operations = [
        migrations.RunPython(
            add_health_wellness_category,
            remove_health_wellness_category,
        ),
    ]
