"""Seed the catalog with reference data and a handful of demo apps.

Idempotent: skips records that already exist, so it is safe to run on
every container start. Used by docker/entrypoint.sh for first-boot.

Usage:
    python manage.py seed_demo               # references + demo apps
    python manage.py seed_demo --refs-only   # references only
"""
from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import App, Category, Platform


DEMO_APPS = [
    {
        "name": "AI Code Assistant",
        "slug": "ai-code-assistant",
        "short_description": "Intelligent coding assistant with multi-language support.",
        "long_description": (
            "AI Code Assistant uses advanced LLMs to provide intelligent code completion, "
            "refactoring suggestions, and bug detection across Python, TypeScript, Go and Rust."
        ),
        "developer_name": "CodeCraft Labs",
        "official_page_url": "https://example.com/ai-code-assistant",
        "quality_score": 95,
        "platforms": ["chatgpt", "claude"],
        "categories": ["developer-tools"],
    },
    {
        "name": "Smart Task Manager",
        "slug": "smart-task-manager",
        "short_description": "AI task management with auto-priority and smart scheduling.",
        "long_description": (
            "Smart Task Manager leverages AI to automatically prioritize your tasks, suggest "
            "optimal schedules, and predict deadlines based on your work patterns."
        ),
        "developer_name": "ProductivityPro",
        "official_page_url": "https://example.com/smart-task-manager",
        "quality_score": 88,
        "platforms": ["chatgpt"],
        "categories": ["productivity"],
    },
    {
        "name": "Data Analyzer Pro",
        "slug": "data-analyzer-pro",
        "short_description": "Advanced data analysis and visualization powered by AI.",
        "long_description": (
            "Data Analyzer Pro transforms raw data into actionable insights using cutting-edge "
            "AI algorithms and beautiful visualizations."
        ),
        "developer_name": "DataTech Solutions",
        "official_page_url": "https://example.com/data-analyzer-pro",
        "quality_score": 92,
        "platforms": ["claude", "mcp"],
        "categories": ["data-analytics", "research"],
    },
    {
        "name": "Neural Writer",
        "slug": "neural-writer",
        "short_description": "AI writing assistant for content creators and marketers.",
        "long_description": (
            "Neural Writer combines GPT-class models with editorial expertise to produce "
            "polished, on-brand content in seconds."
        ),
        "developer_name": "WriteAI Inc",
        "official_page_url": "https://example.com/neural-writer",
        "quality_score": 90,
        "platforms": ["chatgpt", "claude"],
        "categories": ["marketing"],
    },
    {
        "name": "CyberShield MCP",
        "slug": "cybershield-mcp",
        "short_description": "MCP server with security analysis and threat detection.",
        "long_description": (
            "CyberShield MCP exposes vulnerability scanners, OSINT lookups, and intrusion "
            "detection signals to your AI assistant via the Model Context Protocol."
        ),
        "developer_name": "SecureLLM",
        "official_page_url": "https://example.com/cybershield-mcp",
        "quality_score": 87,
        "platforms": ["mcp", "claude"],
        "categories": ["developer-tools"],
    },
    {
        "name": "PromptForge",
        "slug": "promptforge",
        "short_description": "Prompt engineering toolkit with version control and A/B testing.",
        "long_description": (
            "PromptForge gives prompt engineers a Git-like workflow: branches, diffs, evals, "
            "and rollback for every prompt change."
        ),
        "developer_name": "ForgeWorks",
        "official_page_url": "https://example.com/promptforge",
        "quality_score": 84,
        "platforms": ["chatgpt", "claude"],
        "categories": ["developer-tools", "productivity"],
    },
]


class Command(BaseCommand):
    help = "Load reference fixtures and a handful of demo apps. Idempotent."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--refs-only",
            action="store_true",
            help="Load only platforms / categories / capabilities — no demo apps.",
        )

    def handle(self, *args, **options) -> None:
        self._load_references()

        if options["refs_only"]:
            self.stdout.write(self.style.SUCCESS("References loaded; skipping demo apps."))
            return

        self._load_demo_apps()

    def _load_references(self) -> None:
        if Platform.objects.exists() and Category.objects.exists():
            self.stdout.write("References already present — skipping fixture load.")
            return

        self.stdout.write("Loading catalog/fixtures/seed.json ...")
        call_command("loaddata", "seed.json", verbosity=0)
        self.stdout.write(self.style.SUCCESS(
            f"Loaded {Platform.objects.count()} platforms, "
            f"{Category.objects.count()} categories."
        ))

    @transaction.atomic
    def _load_demo_apps(self) -> None:
        created = 0
        for data in DEMO_APPS:
            if App.objects.filter(slug=data["slug"]).exists():
                continue

            platform_slugs = data.pop("platforms")
            category_slugs = data.pop("categories")

            app = App.objects.create(status=App.AppStatus.PUBLISHED, **data)
            app.platforms.add(*Platform.objects.filter(slug__in=platform_slugs))
            app.categories.add(*Category.objects.filter(slug__in=category_slugs))
            created += 1

        if created:
            self.stdout.write(self.style.SUCCESS(f"Created {created} demo apps."))
        else:
            self.stdout.write("All demo apps already exist — nothing to do.")
