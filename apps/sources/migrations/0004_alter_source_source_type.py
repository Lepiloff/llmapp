# Generated manually for Phase 3 discovery source labels.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sources", "0003_alter_source_source_type"),
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
                ],
                max_length=40,
            ),
        ),
    ]
