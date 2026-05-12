"""Project-wide template context."""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest


def site_meta(request: HttpRequest) -> dict[str, str]:
    """Expose site identity to every template."""
    return {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_TAGLINE": settings.SITE_TAGLINE,
        "SITE_BASE_URL": settings.SITE_BASE_URL,
        "TURNSTILE_SITE_KEY": settings.TURNSTILE_SITE_KEY,
    }
