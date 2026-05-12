"""User submissions and claim requests.

Architecture refs:
  * docs/architecture.md § 5.3 (Submission, ClaimRequest)
  * docs/business.md § 10 (user-submission flow)
"""
from __future__ import annotations

from django.db import models
from django.db.models import TextChoices
from django.utils import timezone

from apps.core.models import TimeStampedModel


class Submission(TimeStampedModel):
    """User-submitted app suggestion.

    Goes through a review queue; gets converted to an `App` record
    on approval and normalization.
    """

    class Status(TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CONVERTED = "converted", "Converted to app"

    # Basic info (user-provided)
    app_name = models.CharField(max_length=200)
    short_description = models.CharField(max_length=280)
    long_description = models.TextField(blank=True)
    developer_name = models.CharField(max_length=200, blank=True)
    developer_email = models.EmailField()
    official_url = models.URLField()
    install_url = models.URLField(blank=True)
    repo_url = models.URLField(blank=True)

    # Platform info
    suggested_platforms = models.CharField(
        max_length=300,
        help_text="Comma-separated list of platforms (ChatGPT, Claude, etc.)"
    )
    suggested_categories = models.CharField(
        max_length=300,
        help_text="Comma-separated list of categories"
    )

    # Review workflow
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    reviewer_notes = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # Anti-spam
    submitter_ip = models.GenericIPAddressField()
    turnstile_token = models.CharField(max_length=2000, blank=True)

    # Link to created app (if approved and converted)
    created_app = models.ForeignKey(
        "catalog.App",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submissions"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["developer_email"]),
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Submission: {self.app_name} ({self.status})"

    def mark_reviewed(self, status: str, notes: str = "") -> None:
        """Mark submission as reviewed with given status."""
        self.status = status
        self.reviewer_notes = notes
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "reviewer_notes", "reviewed_at"])


class ClaimRequest(TimeStampedModel):
    """Developer claim request for an existing app.

    Allows developers to claim ownership of their apps in the catalog
    and get update permissions.
    """

    class Status(TextChoices):
        PENDING = "pending", "Pending verification"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class AutoCheckStatus(TextChoices):
        PENDING = "pending", "Auto-check pending"
        PASSED = "passed", "Auto-check passed"
        FAILED = "failed", "Auto-check failed"
        SKIPPED = "skipped", "Auto-check skipped"

    # Target app
    app = models.ForeignKey(
        "catalog.App",
        on_delete=models.CASCADE,
        related_name="claim_requests"
    )

    # Claimant info
    claimant_name = models.CharField(max_length=200)
    claimant_email = models.EmailField()
    verification_method = models.CharField(
        max_length=100,
        help_text="How they proved ownership (email, repo access, etc.)"
    )
    evidence = models.TextField(
        help_text="Evidence of ownership (screenshots, emails, etc.)"
    )

    # Auto-check workflow
    auto_check_status = models.CharField(
        max_length=20,
        choices=AutoCheckStatus.choices,
        default=AutoCheckStatus.PENDING
    )
    auto_check_log = models.TextField(blank=True)
    auto_check_at = models.DateTimeField(null=True, blank=True)

    # Review workflow
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    reviewer_notes = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Admin user who made the final decision"
    )

    # Anti-spam
    submitter_ip = models.GenericIPAddressField()
    turnstile_token = models.CharField(max_length=2000, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["app", "status"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["claimant_email"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["app", "claimant_email"],
                name="unique_claim_per_app_email",
                condition=models.Q(status="pending"),
            ),
        ]

    def __str__(self) -> str:
        return f"Claim: {self.app.name} by {self.claimant_email} ({self.status})"

    def mark_reviewed(self, status: str, notes: str = "") -> None:
        """Mark claim request as reviewed with given status."""
        self.status = status
        self.reviewer_notes = notes
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "reviewer_notes", "reviewed_at"])

        # Update app claim status if approved
        if status == self.Status.APPROVED:
            self.app.developer_claim_status = "claimed"
            self.app.contact_email = self.claimant_email
            self.app.save(update_fields=["developer_claim_status", "contact_email"])