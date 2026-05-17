"""SEO app configuration."""
from __future__ import annotations

from django.apps import AppConfig


class SeoConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.seo'
    verbose_name = 'SEO'

    def ready(self) -> None:
        from . import signals  # noqa: F401