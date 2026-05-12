"""Newsletter app configuration."""
from __future__ import annotations

from django.apps import AppConfig


class NewsletterConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.newsletter'
    verbose_name = 'Newsletter'