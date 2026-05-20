# Generated manually for Sprint 4 direct ingest source labels.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sources", "0005_source_last_enriched_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="source",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("manual", "Manual"),
                    ("mcp_registry", "MCP Registry"),
                    ("submission", "User submission"),
                    ("chatgpt_directory", "ChatGPT App Directory"),
                    ("claude_connectors", "Claude Connectors"),
                    ("agent_enrich", "Agent enrichment"),
                    ("rss_discovery", "RSS discovery"),
                    ("github_mcp", "GitHub MCP search"),
                    ("gemini_extensions", "Gemini Extensions"),
                ],
                max_length=40,
            ),
        ),
    ]
