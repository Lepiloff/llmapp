"""Serialize idempotent container bootstrap work through PostgreSQL."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg

LOCK_ID = 4_832_319_441
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def ensure_django_setup() -> None:
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")
    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()


def run_manage(*args: str) -> None:
    subprocess.run(["python", "manage.py", *args], check=True)


def create_superuser_if_configured() -> None:
    username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "")
    email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
    password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
    if not all((username, email, password)):
        print("Skipping superuser bootstrap (DJANGO_SUPERUSER_* env vars not set).")
        return

    ensure_django_setup()

    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    if user_model.objects.filter(is_superuser=True).exists():
        print("Superuser already exists; skipping bootstrap.")
        return

    user_model.objects.create_superuser(
        username=username,
        email=email,
        password=password,
    )
    print(f"Created superuser {username!r}.")


def configure_site_domain() -> None:
    """Keep django.contrib.sites aligned with SITE_BASE_URL.

    Django's sitemap framework uses the Sites table, not only
    settings.SITE_BASE_URL. A fresh DB starts with example.com, which would
    leak into sitemap.xml unless we normalize it during container bootstrap.
    """
    ensure_django_setup()

    from django.conf import settings
    from django.contrib.sites.models import Site

    parsed = urlparse(settings.SITE_BASE_URL)
    domain = parsed.netloc or parsed.path
    if not domain:
        print("SITE_BASE_URL has no domain; skipping django_site update.")
        return

    Site.objects.update_or_create(
        id=settings.SITE_ID,
        defaults={"domain": domain, "name": settings.SITE_NAME},
    )
    print(f"Configured django_site #{settings.SITE_ID}: {domain}")


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            print("Waiting for PostgreSQL bootstrap lock...")
            cursor.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
            print("PostgreSQL bootstrap lock acquired.")
            try:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                cursor.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
                run_manage("migrate", "--noinput")
                run_manage("seed_demo")
                configure_site_domain()
                create_superuser_if_configured()
            finally:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
                print("PostgreSQL bootstrap lock released.")


if __name__ == "__main__":
    main()
