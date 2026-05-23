"""SEO models for metadata and structured data.

Architecture refs:
  * docs/architecture.md § 11 (SEO, newsletter, and link health)
"""
from __future__ import annotations

from django.db import models

from apps.core.models import TimeStampedModel


class SeoPage(TimeStampedModel):
    """SEO metadata for dynamic pages.

    Allows editors to customize SEO metadata for specific pages
    beyond what's automatically generated.
    """

    page_type = models.CharField(
        max_length=50,
        choices=[
            ('home', 'Home Page'),
            ('search', 'Search Page'),
            ('category', 'Category Page'),
            ('platform', 'Platform Page'),
            ('blog', 'Blog Index'),
        ]
    )
    page_identifier = models.CharField(
        max_length=200,
        blank=True,
        help_text="Slug or identifier for specific page (e.g. category slug)"
    )

    # SEO fields
    meta_title = models.CharField(max_length=200)
    meta_description = models.CharField(max_length=300)
    meta_keywords = models.CharField(max_length=500, blank=True)

    # Open Graph
    og_title = models.CharField(max_length=200, blank=True)
    og_description = models.CharField(max_length=300, blank=True)
    og_image = models.ImageField(upload_to="seo/og_images/", blank=True, null=True)

    # Twitter Card
    twitter_title = models.CharField(max_length=200, blank=True)
    twitter_description = models.CharField(max_length=300, blank=True)

    # Schema.org JSON-LD
    json_ld = models.JSONField(
        blank=True,
        default=dict,
        help_text="Additional structured data in JSON-LD format"
    )

    # Status
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("page_type", "page_identifier")
        indexes = [
            models.Index(fields=["page_type", "page_identifier"]),
        ]

    def __str__(self) -> str:
        if self.page_identifier:
            return f"{self.page_type}:{self.page_identifier}"
        return self.page_type


class Redirect(TimeStampedModel):
    """URL redirects for SEO and maintenance.

    Handles 301/302 redirects for moved pages, old URLs, etc.
    """

    from_path = models.CharField(max_length=500, unique=True)
    to_path = models.CharField(max_length=500)
    status_code = models.PositiveSmallIntegerField(
        default=301,
        choices=[
            (301, "301 Permanent"),
            (302, "302 Temporary"),
        ]
    )
    is_active = models.BooleanField(default=True)
    hit_count = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["from_path"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.from_path} → {self.to_path} ({self.status_code})"
