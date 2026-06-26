"""Seed the catalog with reference data (platforms / categories / capabilities).

The demo-app payload was removed after the agent pipeline started
producing real catalog entries — keeping synthetic placeholders alongside
real apps blurred the public catalog. The reference-data path is
unchanged and still runs idempotently on first boot.

Usage:
    python manage.py seed_demo               # references only (was: + demo apps)
    python manage.py seed_demo --refs-only   # same as no args, kept for compat
"""
from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import App, Category, Platform

# Demo apps were removed once the agent pipeline started producing real
# entries. Kept as an empty list so the rest of the command still type-checks;
# restore from git history if a placeholder catalog is ever needed again.
DEMO_APPS: list[dict] = []


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
