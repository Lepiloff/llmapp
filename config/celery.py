"""Celery application entry point."""
from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("llmmarket")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self) -> None:  # pragma: no cover - debugging utility
    """Echo task useful for verifying Celery is wired up."""
    print(f"Request: {self.request!r}")
