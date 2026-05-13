# Generated manually for Phase 2 review outcome tracking.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="needsreviewqueueentry",
            name="review_outcome",
            field=models.CharField(
                choices=[
                    ("pending", "Pending review"),
                    ("accepted", "Accepted by editor"),
                    ("rejected", "Rejected by editor"),
                    ("no_action", "Resolved without applying"),
                    ("published", "Approved and published"),
                ],
                default="pending",
                help_text="Editor decision used for LLM acceptance-rate reporting.",
                max_length=20,
            ),
        ),
        migrations.AddIndex(
            model_name="needsreviewqueueentry",
            index=models.Index(
                fields=["review_outcome", "-created_at"],
                name="agent_needs_review__c4bfb5_idx",
            ),
        ),
    ]
