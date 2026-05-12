"""Catalog domain models.

Implements the entities from docs/architecture.md § 4–5 and
docs/business.md § 5–6.

Key design choices:
  * The trust model is **three independent axes** (platform/editorial/claim);
    never collapse them into a single field.
  * `App.search_vector` is materialized and refreshed by signals — search
    queries hit a single GIN index, no JOINs.
  * `AppPlatform` carries per-platform metadata (region, supported_plans,
    metadata JSON). Type-specific fields (MCP transport, ChatGPT marketplace
    id) live inside `metadata` so adding a new listing type doesn't require
    a migration.
"""
from __future__ import annotations

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Q, TextChoices
from django.utils import timezone
from django.utils.text import slugify

from apps.core.models import TimeStampedModel

from .managers import AppManager, PublishedAppManager


# ---------------------------------------------------------------------------
# Lookup tables — small, fixed-ish lists with admin-managed names.
# ---------------------------------------------------------------------------
class Platform(models.Model):
    """An LLM platform / client ecosystem (ChatGPT, Claude, Gemini, MCP, ...).

    `public_path` is the URL segment used in platform pages and sitemap
    entries; it intentionally differs from `slug` because there is no clean
    rule to derive "claude-connectors" from "claude".
    """

    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)
    public_path = models.CharField(
        max_length=80,
        unique=True,
        help_text=(
            "URL segment used in platform pages and sitemap "
            "(e.g. 'chatgpt-apps', 'claude-connectors', 'mcp-servers')."
        ),
    )
    website_url = models.URLField(blank=True)
    description = models.TextField(blank=True)
    sort_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return f"/{self.public_path}/"


class ListingType(models.Model):
    """Class of catalog entity (ChatGPT App, Claude Connector, MCP Server...)."""

    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    sort_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name


class Category(models.Model):
    """Thematic category (Productivity, Developer Tools, ...). 10 in MVP."""

    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    sort_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return f"/apps/{self.slug}/"


class Capability(models.Model):
    """Structured boolean-with-unknown flag (read_data, write_actions, ...)."""

    key = models.SlugField(unique=True)
    label = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    sort_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "key"]
        verbose_name_plural = "capabilities"

    def __str__(self) -> str:
        return self.label


class UseCase(models.Model):
    """Short free-form phrase ("turn notes into slides")."""

    title = models.CharField(max_length=160, unique=True)
    slug = models.SlugField(unique=True, max_length=200)

    class Meta:
        ordering = ["title"]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs) -> None:
        if not self.slug:
            self.slug = slugify(self.title)[:200]
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# App — the central listing entity.
# ---------------------------------------------------------------------------
class App(TimeStampedModel):
    """Catalog listing. May be an app, a connector, an MCP server, or a mix."""

    # Editorial lifecycle / public visibility. Set by editors only.
    class AppStatus(TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        HIDDEN = "hidden", "Hidden by editor"

    # Three independent trust axes — see business.md § 6.5.
    class PlatformVerificationStatus(TextChoices):
        OFFICIAL = "official", "Listed in official platform directory"
        NOT_LISTED = "not_listed", "Not listed in any platform directory"
        UNKNOWN = "unknown", "Unknown"

    class EditorialReviewStatus(TextChoices):
        REVIEWED = "reviewed", "Reviewed by editor"
        UNREVIEWED = "unreviewed", "Not yet reviewed"

    class DeveloperClaimStatus(TextChoices):
        UNCLAIMED = "unclaimed", "Unclaimed"
        PENDING = "pending", "Claim pending"
        CLAIMED = "claimed", "Verified by developer"

    class LaunchStatus(TextChoices):
        LIVE = "live", "Live"
        BETA = "beta", "Beta"
        WAITLIST = "waitlist", "Waitlist"
        DEPRECATED = "deprecated", "Deprecated"

    class PricingModel(TextChoices):
        FREE = "free", "Free"
        PAID = "paid", "Paid"
        FREEMIUM = "freemium", "Freemium"
        UNKNOWN = "unknown", "Unknown"

    # Identity
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, max_length=220)
    listing_types = models.ManyToManyField(ListingType, related_name="apps", blank=True)

    # Descriptions
    short_description = models.CharField(max_length=280)
    long_description = models.TextField(blank=True)
    verdict = models.CharField(max_length=280, blank=True)

    # People / company
    developer_name = models.CharField(max_length=200, blank=True)
    developer_url = models.URLField(blank=True)
    contact_email = models.EmailField(blank=True)

    # Media
    logo = models.ImageField(upload_to="apps/logos/", blank=True, null=True)
    cover_image = models.ImageField(upload_to="apps/covers/", blank=True, null=True)

    # Links
    official_page_url = models.URLField(blank=True)
    install_url = models.URLField(blank=True)
    repo_url = models.URLField(blank=True)

    # Classification (M2M through models for per-relation metadata)
    platforms = models.ManyToManyField(
        Platform, through="AppPlatform", related_name="apps", blank=True
    )
    categories = models.ManyToManyField(
        Category, through="AppCategory", related_name="apps", blank=True
    )
    capabilities = models.ManyToManyField(
        Capability, through="AppCapability", related_name="apps", blank=True
    )
    use_cases = models.ManyToManyField(
        UseCase, through="AppUseCase", related_name="apps", blank=True
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=AppStatus.choices,
        default=AppStatus.DRAFT,
    )
    platform_verification_status = models.CharField(
        max_length=20,
        choices=PlatformVerificationStatus.choices,
        default=PlatformVerificationStatus.UNKNOWN,
    )
    editorial_review_status = models.CharField(
        max_length=20,
        choices=EditorialReviewStatus.choices,
        default=EditorialReviewStatus.UNREVIEWED,
    )
    developer_claim_status = models.CharField(
        max_length=20,
        choices=DeveloperClaimStatus.choices,
        default=DeveloperClaimStatus.UNCLAIMED,
    )
    launch_status = models.CharField(
        max_length=20,
        choices=LaunchStatus.choices,
        default=LaunchStatus.LIVE,
    )
    pricing_model = models.CharField(
        max_length=20,
        choices=PricingModel.choices,
        default=PricingModel.UNKNOWN,
    )

    # Flags
    is_featured = models.BooleanField(default=False)
    is_indexable = models.BooleanField(default=True)

    # Quality (computed; range enforced via CheckConstraint)
    quality_score = models.PositiveSmallIntegerField(default=0)

    # SEO
    meta_title = models.CharField(max_length=200, blank=True)
    meta_description = models.CharField(max_length=300, blank=True)

    # Search — written exclusively by `apps.search.vector.refresh_search_vector`.
    search_index_text = models.TextField(blank=True, default="")
    search_vector = SearchVectorField(null=True, blank=True)

    # Timestamps
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    # Managers — `published` is the public-visibility gate.
    objects: AppManager = AppManager()
    published: PublishedAppManager = PublishedAppManager()

    class Meta:
        indexes = [
            models.Index(fields=["status", "-quality_score"]),
            models.Index(fields=["-first_seen_at"]),
            models.Index(fields=["is_featured", "-quality_score"]),
            GinIndex(fields=["search_vector"], name="app_search_vector_gin"),
            GinIndex(
                name="app_name_trgm_gin",
                fields=["name"],
                opclasses=["gin_trgm_ops"],
            ),
            GinIndex(
                name="app_dev_trgm_gin",
                fields=["developer_name"],
                opclasses=["gin_trgm_ops"],
            ),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(quality_score__gte=0) & Q(quality_score__lte=100),
                name="app_quality_score_range",
            ),
        ]
        ordering = ["-quality_score", "-first_seen_at"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return f"/apps/{self.slug}/"

    @property
    def is_published(self) -> bool:
        return self.status == self.AppStatus.PUBLISHED and self.is_indexable

    @property
    def has_official_platform_url(self) -> bool:
        """`platform_verification_status = OFFICIAL` requires a directory URL."""
        return (
            self.platform_links.exclude(official_directory_url="")
            .exists()
        )


# ---------------------------------------------------------------------------
# Through models — per-relation metadata.
# ---------------------------------------------------------------------------
class AppPlatform(models.Model):
    """How a given `App` is available on a `Platform`.

    Top-level columns are platform-agnostic. Type-specific bits (MCP transport,
    ChatGPT marketplace id, ...) live in `metadata` so we don't pollute the
    schema with one column per listing type.
    """

    class CompatibilityStatus(TextChoices):
        SUPPORTED = "supported", "Supported"
        PARTIAL = "partial", "Partial"
        PREVIEW = "preview", "Preview"
        UNKNOWN = "unknown", "Unknown"

    class RegionAvailability(TextChoices):
        WORLDWIDE = "worldwide", "Worldwide"
        US_ONLY = "us_only", "US only"
        EU_ONLY = "eu_only", "EU only"
        EXCLUDES_EEA = "excludes_eea", "Excludes EEA"
        LIMITED = "limited", "Limited (see notes)"
        UNKNOWN = "unknown", "Unknown"

    app = models.ForeignKey(
        App, on_delete=models.CASCADE, related_name="platform_links"
    )
    platform = models.ForeignKey(
        Platform, on_delete=models.CASCADE, related_name="app_links"
    )

    compatibility_status = models.CharField(
        max_length=20,
        choices=CompatibilityStatus.choices,
        default=CompatibilityStatus.SUPPORTED,
    )

    # Single source of truth for plan support: list, not scalar.
    supported_plans = models.JSONField(default=list, blank=True)
    region_availability = models.CharField(
        max_length=20,
        choices=RegionAvailability.choices,
        default=RegionAvailability.UNKNOWN,
    )
    supported_models = models.CharField(
        max_length=300,
        blank=True,
        help_text="Free text. Models the listing works with.",
    )
    scope_summary = models.CharField(
        max_length=280,
        blank=True,
        help_text="Human summary of the permissions the integration requests.",
    )

    # Links
    official_directory_url = models.URLField(blank=True)
    install_url = models.URLField(blank=True)

    # Type-specific JSON bag.
    metadata = models.JSONField(default=dict, blank=True)

    notes = models.CharField(max_length=300, blank=True)
    last_verified_on_platform_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("app", "platform")
        indexes = [
            models.Index(fields=["platform", "app"]),
            models.Index(fields=["region_availability"]),
        ]

    def __str__(self) -> str:
        return f"{self.app} ↔ {self.platform}"


class AppCategory(models.Model):
    app = models.ForeignKey(App, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    is_primary = models.BooleanField(default=False)

    class Meta:
        unique_together = ("app", "category")
        indexes = [models.Index(fields=["category", "app"])]


class AppCapability(models.Model):
    class CapabilityValue(TextChoices):
        YES = "yes", "Yes"
        NO = "no", "No"
        UNKNOWN = "unknown", "Unknown"

    app = models.ForeignKey(App, on_delete=models.CASCADE)
    capability = models.ForeignKey(Capability, on_delete=models.CASCADE)
    value = models.CharField(
        max_length=10,
        choices=CapabilityValue.choices,
        default=CapabilityValue.UNKNOWN,
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ("app", "capability")
        indexes = [models.Index(fields=["capability", "value"])]

    def __str__(self) -> str:
        return f"{self.app}.{self.capability.key}={self.value}"


class AppUseCase(models.Model):
    app = models.ForeignKey(App, on_delete=models.CASCADE)
    use_case = models.ForeignKey(UseCase, on_delete=models.CASCADE)

    class Meta:
        unique_together = ("app", "use_case")
