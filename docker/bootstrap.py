"""Serialize idempotent container bootstrap work through PostgreSQL."""
from __future__ import annotations

import os
import subprocess

import psycopg


LOCK_ID = 4_832_319_441


def run_manage(*args: str) -> None:
    subprocess.run(["python", "manage.py", *args], check=True)


def create_superuser_if_configured() -> None:
    username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "")
    email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
    password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
    if not all((username, email, password)):
        print("Skipping superuser bootstrap (DJANGO_SUPERUSER_* env vars not set).")
        return

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")
    import django

    django.setup()

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
                create_superuser_if_configured()
            finally:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
                print("PostgreSQL bootstrap lock released.")


if __name__ == "__main__":
    main()
