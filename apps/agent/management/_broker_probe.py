"""Broker-reachability probe for host-venv management commands.

The agent's pipeline runs through Celery via ``shared_task`` definitions.
When ``CELERY_TASK_ALWAYS_EAGER`` is False (production default) Celery
needs an actual broker connection to dispatch work. The docker-compose
broker hostname (``redis``) doesn't resolve from a host-venv shell, so
running ``manage.py agent_run --apply`` outside the container blew up
inside a worker dispatch with a misleading "Connection lost" stack.

This module exists so the management command can detect that situation
before any work starts and either auto-flip to eager mode (so the
host-venv operator gets a working command without env tweaks) or fail
fast with a clean message pointing to the container path. Closes the
F4 known-issue from the rollout log.
"""
from __future__ import annotations

import logging
import socket
from urllib.parse import urlparse

from django.conf import settings

logger = logging.getLogger(__name__)


def broker_host_port(broker_url: str) -> tuple[str, int] | None:
    """Extract (host, port) from a ``redis://host:port/db`` URL.

    Returns None for URL shapes we don't recognise — the caller treats
    "can't parse" as "can't probe", which falls through to the existing
    Celery dispatch error path (loud, but at least informative).
    """
    parsed = urlparse(broker_url)
    if parsed.scheme not in {"redis", "rediss"}:
        return None
    host = parsed.hostname or ""
    if not host:
        return None
    port = parsed.port or (6380 if parsed.scheme == "rediss" else 6379)
    return host, port


def is_broker_reachable(*, timeout: float = 1.0) -> bool:
    """Open a quick TCP connection to the configured broker.

    Treats both DNS failure and TCP connect failure as "unreachable".
    The probe is intentionally short — we'd rather fall through to
    eager mode than block command startup for 30s on a routing issue.
    """
    target = broker_host_port(settings.CELERY_BROKER_URL)
    if target is None:
        return True  # unknown shape — let Celery handle it the old way
    host, port = target
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.gaierror, OSError):
        return False


def ensure_eager_if_broker_unreachable(stderr=None) -> bool:
    """If the broker can't be reached, flip Celery to eager mode in-process.

    Returns True when this function actually changed the mode; False
    when no change was needed (either broker reachable, or eager was
    already on, or the URL shape isn't probable). Writes a one-line
    warning to ``stderr`` so the operator notices the auto-fallback —
    silence here would hide the fact that beat-style scheduling is
    no longer in effect.

    Called from ``manage.py agent_run`` BEFORE any Celery dispatch.
    Safe to call when running inside the container: the docker
    hostname resolves, the probe returns True, nothing happens.

    The flip writes to Django settings because Celery's app.conf reads
    from settings via ``config_from_object("django.conf:settings",
    namespace="CELERY")`` — setting ``app.conf.task_always_eager``
    directly does NOT propagate (Celery resolves the value back through
    Django settings at task-dispatch time).
    """
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return False
    if is_broker_reachable():
        return False

    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True

    target = broker_host_port(settings.CELERY_BROKER_URL)
    where = f"{target[0]}:{target[1]}" if target else settings.CELERY_BROKER_URL
    message = (
        f"⚠ Broker {where} unreachable — auto-enabling "
        "CELERY_TASK_ALWAYS_EAGER for this command. "
        "For production-shaped runs, exec inside the container: "
        "`docker compose exec -T web python manage.py agent_run ...`"
    )
    if stderr is not None:
        stderr.write(message + "\n")
    logger.warning(
        "agent_run_eager_fallback",
        extra={"broker_url": settings.CELERY_BROKER_URL},
    )
    return True
