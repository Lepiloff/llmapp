"""Development settings — friendly defaults, no external dependencies required."""
from __future__ import annotations

from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Console email in dev keeps mailbox spam from local runs.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Allow loading static assets from arbitrary dev origins.
CORS_ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
]
