"""Newsletter models for email subscriptions and issues.

Architecture refs:
  * docs/architecture.md § 11 (newsletter system)
  * docs/business.md § 12 (newsletter strategy)
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.core.validators import EmailValidator
from django.db import models
from django.db.models import TextChoices
from django.utils import timezone
from django.urls import reverse

from apps.core.models import TimeStampedModel


class Subscriber(TimeStampedModel):
    """Newsletter subscriber with double opt-in confirmation.

    Tracks subscription status and preferences for email campaigns.
    """

    class Status(TextChoices):
        PENDING = "pending", "Pending confirmation"
        ACTIVE = "active", "Active"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        BOUNCED = "bounced", "Email bounced"

    email = models.EmailField(unique=True, validators=[EmailValidator()])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Confirmation
    confirmation_token = models.UUIDField(default=uuid.uuid4, unique=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    # Subscription preferences
    frequency = models.CharField(
        max_length=20,
        choices=[
            ('weekly', 'Weekly'),
            ('biweekly', 'Bi-weekly'),
        ],
        default='weekly'
    )

    # Analytics
    source = models.CharField(
        max_length=100,
        blank=True,
        help_text="How they found us (home page, blog post, etc.)"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    # Engagement tracking
    last_opened_at = models.DateTimeField(null=True, blank=True)
    total_opens = models.PositiveIntegerField(default=0)
    total_clicks = models.PositiveIntegerField(default=0)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["email"]),
        ]

    def __str__(self) -> str:
        return f"{self.email} ({self.status})"

    def confirm_subscription(self) -> None:
        """Confirm email subscription."""
        self.status = self.Status.ACTIVE
        self.confirmed_at = timezone.now()
        self.save(update_fields=['status', 'confirmed_at'])

    def unsubscribe(self) -> None:
        """Unsubscribe from newsletter."""
        self.status = self.Status.UNSUBSCRIBED
        self.unsubscribed_at = timezone.now()
        self.save(update_fields=['status', 'unsubscribed_at'])

    def get_confirmation_url(self) -> str:
        """Get email confirmation URL."""
        return reverse('newsletter:confirm', kwargs={'token': self.confirmation_token})

    def get_unsubscribe_url(self) -> str:
        """Get unsubscribe URL."""
        return reverse('newsletter:unsubscribe', kwargs={'token': self.confirmation_token})

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE


class Issue(TimeStampedModel):
    """Newsletter issue/edition.

    Each issue contains curated content and is sent to active subscribers.
    """

    class Status(TextChoices):
        DRAFT = "draft", "Draft"
        SCHEDULED = "scheduled", "Scheduled"
        SENT = "sent", "Sent"
        CANCELLED = "cancelled", "Cancelled"

    # Content
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, max_length=220)
    subject_line = models.CharField(max_length=200)
    preheader = models.CharField(
        max_length=150,
        blank=True,
        help_text="Preview text shown in email clients"
    )

    # Issue content sections
    intro_text = models.TextField(blank=True)
    featured_apps = models.ManyToManyField(
        "catalog.App",
        through="IssueApp",
        related_name="newsletter_issues",
        blank=True
    )
    conclusion_text = models.TextField(blank=True)

    # Publication
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    # Analytics
    recipient_count = models.PositiveIntegerField(default=0)
    delivered_count = models.PositiveIntegerField(default=0)
    opened_count = models.PositiveIntegerField(default=0)
    clicked_count = models.PositiveIntegerField(default=0)
    bounced_count = models.PositiveIntegerField(default=0)
    unsubscribed_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["-sent_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.status})"

    def get_absolute_url(self) -> str:
        return reverse('newsletter:issue_detail', kwargs={'slug': self.slug})

    @property
    def open_rate(self) -> float:
        if self.delivered_count == 0:
            return 0.0
        return (self.opened_count / self.delivered_count) * 100

    @property
    def click_rate(self) -> float:
        if self.delivered_count == 0:
            return 0.0
        return (self.clicked_count / self.delivered_count) * 100

    def mark_as_sent(self) -> None:
        """Mark issue as sent."""
        self.status = self.Status.SENT
        self.sent_at = timezone.now()
        self.save(update_fields=['status', 'sent_at'])


class IssueApp(models.Model):
    """Through model for Issue <-> App relationship with editorial content."""

    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)
    app = models.ForeignKey("catalog.App", on_delete=models.CASCADE)
    sort_order = models.PositiveSmallIntegerField(default=100)
    description = models.TextField(
        help_text="Editorial description of why this app is featured"
    )

    class Meta:
        unique_together = ("issue", "app")
        ordering = ["sort_order", "id"]


class EmailClick(TimeStampedModel):
    """Track clicks in newsletter emails."""

    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="email_clicks")
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name="email_clicks")

    # What was clicked
    link_type = models.CharField(
        max_length=20,
        choices=[
            ('app', 'App link'),
            ('blog', 'Blog post link'),
            ('website', 'Website link'),
            ('unsubscribe', 'Unsubscribe link'),
        ]
    )
    target_url = models.URLField()
    app = models.ForeignKey(
        "catalog.App",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="App that was clicked (if applicable)"
    )

    # Context
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["issue", "-created_at"]),
            models.Index(fields=["subscriber", "-created_at"]),
        ]


class EmailOpen(TimeStampedModel):
    """Track email opens."""

    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="email_opens")
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name="email_opens")

    # Context
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["issue", "-created_at"]),
            models.Index(fields=["subscriber", "-created_at"]),
        ]
        # Prevent duplicate opens from the same subscriber for the same issue
        unique_together = ("issue", "subscriber")