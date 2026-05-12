"""Editorial content models - blog posts, collections, comparisons.

Architecture refs:
  * docs/architecture.md § 10 (editorial content)
  * docs/business.md § 8 (blog content strategy)
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q, TextChoices
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from apps.core.models import TimeStampedModel


class Post(TimeStampedModel):
    """Editorial blog posts for SEO and user engagement.

    These are how-to guides, lists, comparisons, and other content
    that drives organic traffic and provides value to users.
    """

    class Status(TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    class PostType(TextChoices):
        ARTICLE = "article", "Article"
        GUIDE = "guide", "How-to Guide"
        COMPARISON = "comparison", "Comparison"
        ROUNDUP = "roundup", "App Roundup"
        NEWS = "news", "News"

    # Content
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, max_length=220)
    subtitle = models.CharField(max_length=300, blank=True)
    excerpt = models.TextField(
        max_length=500,
        help_text="Brief summary for social media and search results"
    )
    content = models.TextField()

    # Metadata
    post_type = models.CharField(max_length=20, choices=PostType.choices, default=PostType.ARTICLE)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    # SEO
    meta_title = models.CharField(max_length=200, blank=True)
    meta_description = models.CharField(max_length=300, blank=True)

    # Publishing
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name="posts")
    published_at = models.DateTimeField(null=True, blank=True)
    featured_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Keep featured on homepage until this date"
    )

    # Media
    cover_image = models.ImageField(upload_to="editorial/covers/", blank=True, null=True)
    cover_alt_text = models.CharField(max_length=200, blank=True)

    # Relationships
    related_apps = models.ManyToManyField(
        "catalog.App",
        through="PostApp",
        related_name="editorial_posts",
        blank=True
    )
    tags = models.ManyToManyField("Tag", related_name="posts", blank=True)

    # Performance
    view_count = models.PositiveIntegerField(default=0)
    reading_time_minutes = models.PositiveSmallIntegerField(
        default=5,
        help_text="Estimated reading time in minutes"
    )

    class Meta:
        indexes = [
            models.Index(fields=["status", "-published_at"]),
            models.Index(fields=["-published_at"]),
            models.Index(fields=["post_type", "-published_at"]),
            models.Index(fields=["featured_until"]),
        ]
        ordering = ["-published_at", "-created_at"]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)[:220]

        # Auto-generate meta fields if not set
        if not self.meta_title:
            self.meta_title = self.title[:200]
        if not self.meta_description:
            self.meta_description = self.excerpt[:300]

        # Set published_at when status changes to published
        if self.status == self.Status.PUBLISHED and not self.published_at:
            self.published_at = timezone.now()

        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("editorial:post_detail", kwargs={"slug": self.slug})

    @property
    def is_published(self) -> bool:
        return self.status == self.Status.PUBLISHED and self.published_at

    @property
    def is_featured(self) -> bool:
        if not self.featured_until:
            return False
        return timezone.now() <= self.featured_until

    def increment_view_count(self) -> None:
        """Increment view count (call from views)."""
        self.__class__.objects.filter(pk=self.pk).update(view_count=models.F('view_count') + 1)


class Collection(TimeStampedModel):
    """Curated collections of apps around a theme.

    These are evergreen content pieces that showcase groups of apps
    for specific use cases or categories.
    """

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, max_length=220)
    description = models.TextField()
    subtitle = models.CharField(max_length=300, blank=True)

    # Content
    intro_text = models.TextField(blank=True, help_text="Introduction text for the collection")
    conclusion_text = models.TextField(blank=True, help_text="Conclusion text for the collection")

    # Publishing
    is_published = models.BooleanField(default=False)
    curator = models.ForeignKey(User, on_delete=models.PROTECT, related_name="collections")

    # SEO
    meta_title = models.CharField(max_length=200, blank=True)
    meta_description = models.CharField(max_length=300, blank=True)

    # Media
    cover_image = models.ImageField(upload_to="editorial/collections/", blank=True, null=True)

    # Apps in this collection
    apps = models.ManyToManyField(
        "catalog.App",
        through="CollectionApp",
        related_name="collections"
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:220]

        if not self.meta_title:
            self.meta_title = self.name[:200]
        if not self.meta_description:
            self.meta_description = self.description[:300]

        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("editorial:collection_detail", kwargs={"slug": self.slug})


class Comparison(TimeStampedModel):
    """Head-to-head comparisons between apps.

    Useful for SEO (vs. queries) and helping users choose
    between similar tools.
    """

    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, max_length=220)
    introduction = models.TextField()
    conclusion = models.TextField()

    # Apps being compared
    primary_app = models.ForeignKey(
        "catalog.App",
        on_delete=models.CASCADE,
        related_name="comparisons_as_primary"
    )
    secondary_app = models.ForeignKey(
        "catalog.App",
        on_delete=models.CASCADE,
        related_name="comparisons_as_secondary"
    )

    # Analysis sections
    criteria = models.JSONField(
        default=dict,
        help_text="Comparison criteria and scores in JSON format"
    )

    # Publishing
    is_published = models.BooleanField(default=False)
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name="comparisons")

    # SEO
    meta_title = models.CharField(max_length=200, blank=True)
    meta_description = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["primary_app", "secondary_app"],
                name="unique_comparison_pair"
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.primary_app.name} vs {self.secondary_app.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            slug_base = f"{self.primary_app.slug}-vs-{self.secondary_app.slug}"
            self.slug = slug_base[:220]

        if not self.meta_title:
            self.meta_title = f"{self.primary_app.name} vs {self.secondary_app.name}"[:200]

        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("editorial:comparison_detail", kwargs={"slug": self.slug})


class Tag(models.Model):
    """Tags for categorizing editorial content."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True, max_length=120)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:120]
        super().save(*args, **kwargs)


# Through models for many-to-many relationships with extra fields

class PostApp(models.Model):
    """Through model for Post <-> App relationship."""

    post = models.ForeignKey(Post, on_delete=models.CASCADE)
    app = models.ForeignKey("catalog.App", on_delete=models.CASCADE)
    sort_order = models.PositiveSmallIntegerField(default=100)
    note = models.CharField(
        max_length=300,
        blank=True,
        help_text="Editorial note about this app in the context of the post"
    )

    class Meta:
        unique_together = ("post", "app")
        ordering = ["sort_order", "id"]


class CollectionApp(models.Model):
    """Through model for Collection <-> App relationship."""

    collection = models.ForeignKey(Collection, on_delete=models.CASCADE)
    app = models.ForeignKey("catalog.App", on_delete=models.CASCADE)
    sort_order = models.PositiveSmallIntegerField(default=100)
    description = models.TextField(
        blank=True,
        help_text="Why this app is included in the collection"
    )

    class Meta:
        unique_together = ("collection", "app")
        ordering = ["sort_order", "id"]