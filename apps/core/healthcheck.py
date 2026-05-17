"""Liveness/readiness endpoint — used by the load balancer and uptime probes.

Architecture ref: docs/architecture.md § 15.3.
"""
from __future__ import annotations

import logging

from django.db import connection
from django.http import HttpRequest, JsonResponse

from django_redis import get_redis_connection

logger = logging.getLogger(__name__)

# Celery worker probe budget. Short enough to keep /health/ snappy under a
# load balancer; long enough for a healthy worker to respond.
_CELERY_PING_TIMEOUT_SECONDS = 1.5


def _check_db() -> bool:
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:  # pragma: no cover - infra failure
        logger.exception("healthcheck_db_failed")
        return False


def _check_redis() -> bool:
    try:
        get_redis_connection("default").ping()
        return True
    except Exception:  # pragma: no cover - infra failure
        logger.exception("healthcheck_redis_failed")
        return False


def _check_pg_trgm() -> bool:
    """Verify the `pg_trgm` extension is loaded.

    A missing extension would make every fuzzy-search query 500.
    """
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT similarity('a'::text, 'a'::text)")
            row = cur.fetchone()
            return bool(row and row[0] == 1)
    except Exception:  # pragma: no cover - infra failure
        logger.exception("healthcheck_pg_trgm_failed")
        return False


def _check_celery_worker() -> bool:
    """Return True when at least one Celery worker replies to a ping.

    A healthy ``web`` container in front of a dead ``worker`` would happily
    accept traffic — beat-scheduled discovery, re-actualization and budget
    checks would all silently stop. We probe via the standard control
    channel with a short timeout so /health/ stays snappy under the load
    balancer.
    """
    try:
        from config.celery import app as celery_app

        replies = celery_app.control.ping(timeout=_CELERY_PING_TIMEOUT_SECONDS)
    except Exception:  # pragma: no cover - infra failure (broker down / import)
        logger.exception("healthcheck_celery_failed")
        return False
    return bool(replies)


def view(request: HttpRequest) -> JsonResponse:
    """Return 200 when every dependency is healthy, 503 otherwise."""
    checks = {
        "db": _check_db(),
        "redis": _check_redis(),
        "pg_trgm": _check_pg_trgm(),
        "celery_worker": _check_celery_worker(),
    }
    status = 200 if all(checks.values()) else 503
    return JsonResponse(
        {"status": "ok" if status == 200 else "fail", "checks": checks},
        status=status,
    )
