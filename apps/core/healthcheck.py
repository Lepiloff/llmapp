"""Liveness/readiness endpoint — used by the load balancer and uptime probes.

Architecture ref: docs/architecture.md § 15.3.
"""
from __future__ import annotations

import logging

from django.db import connection
from django.http import HttpRequest, JsonResponse

from django_redis import get_redis_connection

logger = logging.getLogger(__name__)


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
            cur.execute("SELECT 'a' %% 'a'")
            cur.fetchone()
        return True
    except Exception:  # pragma: no cover - infra failure
        logger.exception("healthcheck_pg_trgm_failed")
        return False


def view(request: HttpRequest) -> JsonResponse:
    """Return 200 when every dependency is healthy, 503 otherwise."""
    checks = {
        "db": _check_db(),
        "redis": _check_redis(),
        "pg_trgm": _check_pg_trgm(),
    }
    status = 200 if all(checks.values()) else 503
    return JsonResponse(
        {"status": "ok" if status == 200 else "fail", "checks": checks},
        status=status,
    )
