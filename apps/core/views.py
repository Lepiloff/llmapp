"""Core app views - health checks and utilities."""
from __future__ import annotations

from django.http import HttpRequest, JsonResponse

from . import healthcheck


def health_check(request: HttpRequest) -> JsonResponse:
    """Main health check endpoint - all dependencies."""
    return healthcheck.view(request)


def health_check_db(request: HttpRequest) -> JsonResponse:
    """Database-only health check."""
    db_ok = healthcheck._check_db()
    status = 200 if db_ok else 503
    return JsonResponse(
        {"status": "ok" if db_ok else "fail", "db": db_ok},
        status=status,
    )


def health_check_cache(request: HttpRequest) -> JsonResponse:
    """Cache-only health check."""
    cache_ok = healthcheck._check_redis()
    status = 200 if cache_ok else 503
    return JsonResponse(
        {"status": "ok" if cache_ok else "fail", "redis": cache_ok},
        status=status,
    )