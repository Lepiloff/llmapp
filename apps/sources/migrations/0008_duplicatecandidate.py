import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0002_expand_capability_note_to_500"),
        ("sources", "0007_alter_source_source_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="DuplicateCandidate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("match_reason", models.CharField(max_length=80)),
                ("score", models.FloatField(default=0)),
                ("evidence", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending review"),
                            ("confirmed", "Confirmed duplicate"),
                            ("dismissed", "Not a duplicate"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "app",
                    models.ForeignKey(
                        help_text="Newly created draft that may duplicate candidate_app.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="duplicate_candidates",
                        to="catalog.app",
                    ),
                ),
                (
                    "candidate_app",
                    models.ForeignKey(
                        help_text="Existing app that may be the same product.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="duplicate_suspicions",
                        to="catalog.app",
                    ),
                ),
                (
                    "source",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="duplicate_candidates",
                        to="sources.source",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["status", "-created_at"],
                        name="sources_dup_status_idx",
                    ),
                    models.Index(
                        fields=["app", "candidate_app"],
                        name="sources_dup_apps_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("app", "candidate_app", "match_reason"),
                        name="duplicate_candidate_once_per_reason",
                    ),
                ],
            },
        ),
    ]
