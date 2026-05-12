"""Shared abstract models reused across the project."""
from __future__ import annotations

from django.db import models


class TimeStampedModel(models.Model):
    """Adds `created_at` / `updated_at` timestamps automatically.

    Use as a base for value-objects that need creation and update tracking
    without re-declaring two boilerplate fields.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
