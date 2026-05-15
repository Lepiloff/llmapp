"""Operational models for the LLM-pipeline agent.

These tables exist so editors and operators can answer four questions
without spelunking through Sentry / Celery logs:

* **What did the agent do?** — ``AgentRun`` (one row per pipeline batch).
* **What did it try on each app?** — ``EnrichmentTask`` (one row per app
  inside a run).
* **What did we spend on LLM calls?** — ``LLMCallLog`` (one row per
  provider request; rolls up into a monthly cost dashboard).
* **What needs human review?** — ``NeedsReviewQueueEntry`` (one row per
  proposed change the agent declined to apply automatically).

These models intentionally live in the Django app rather than in the
pure-Python pipeline package: when the agent is extracted to its own
service (see ``docs/agent-pipeline.md`` "Future"), these orchestration
tables migrate with the service to its own database; nothing in
``apps.catalog`` depends on them.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone


class AgentRun(models.Model):
    """One pipeline run — a batch with a single trigger and a single budget."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        DRY_RUN = "dry_run", "Dry-run (no DB writes)"

    class Trigger(models.TextChoices):
        BEAT = "beat", "Celery beat"
        MANUAL = "manual", "Manual command"
        ADMIN = "admin", "Admin action"

    source_type = models.CharField(
        max_length=40,
        help_text=(
            "Logical source identifier — typically the source_type used by"
            " apps.sources (e.g. 'mcp_registry'), or a discovery source"
            " (e.g. 'rss', 'github_mcp')."
        ),
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    trigger = models.CharField(
        max_length=20, choices=Trigger.choices, default=Trigger.MANUAL
    )
    triggered_by = models.CharField(
        max_length=120, blank=True,
        help_text="Username or beat-task slug; informational, not auth.",
    )
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    total_cost_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=0,
        help_text="Sum of LLMCallLog.cost_usd across this run's tasks.",
    )
    stats = models.JSONField(
        default=dict, blank=True,
        help_text="Free-form counters: drafts_enriched, capabilities_added, etc.",
    )
    error = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["-started_at"]),
            models.Index(fields=["status", "-started_at"]),
            models.Index(fields=["source_type", "-started_at"]),
        ]
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"AgentRun #{self.pk} {self.source_type} [{self.status}]"


class EnrichmentTask(models.Model):
    """One enrichment attempt — typically one App per task."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        FETCHING = "fetching", "Fetching source"
        ENRICHING = "enriching", "Calling LLM"
        VALIDATING = "validating", "Validating LLM output"
        PERSISTED = "persisted", "Persisted to DB"
        DRY_RUN = "dry_run", "Dry-run (not persisted)"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped (e.g. dedup)"

    run = models.ForeignKey(
        AgentRun, on_delete=models.CASCADE, related_name="tasks"
    )
    app = models.ForeignKey(
        "catalog.App", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enrichment_tasks",
        help_text=(
            "Nullable: discovery tasks operate on a URL before any App is"
            " created."
        ),
    )
    source_url = models.URLField(blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    diff_summary = models.JSONField(
        default=dict, blank=True,
        help_text=(
            "Snapshot of what the agent proposed vs the previous state."
            " Persisted for both real and dry-run modes — auditable trace"
            " of every LLM decision the editor will see."
        ),
    )

    class Meta:
        indexes = [
            models.Index(fields=["run", "status"]),
            models.Index(fields=["app", "-started_at"]),
            models.Index(fields=["-started_at"]),
        ]
        ordering = ["-started_at"]

    def __str__(self) -> str:
        app_part = f"app={self.app_id}" if self.app_id else "no-app"
        return f"EnrichmentTask #{self.pk} {app_part} [{self.status}]"


class LLMCallLog(models.Model):
    """One LLM provider request — the unit of cost and audit."""

    task = models.ForeignKey(
        EnrichmentTask, on_delete=models.CASCADE, related_name="llm_calls"
    )
    provider = models.CharField(max_length=40)
    model = models.CharField(max_length=80)
    prompt_version = models.CharField(
        max_length=20, blank=True,
        help_text="Versioned prompt key — e.g. 'enrich-v1.0'.",
    )
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cached_tokens = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Tokens served from prompt cache (OpenAI prompt cache or "
            "Anthropic cache_read). Subtracted from billable input before "
            "the cached-input price is applied. 0 for mock provider."
        ),
    )
    cost_usd = models.DecimalField(
        max_digits=10, decimal_places=6, default=0,
        help_text="Per-call cost computed from provider pricing at request time.",
    )
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    is_mock = models.BooleanField(
        default=False,
        help_text="True when the call was served by MockLLMProvider in tests / dry-run.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["task", "created_at"]),
            models.Index(fields=["-created_at"]),
            models.Index(fields=["provider", "model"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"LLMCall #{self.pk} {self.provider}/{self.model} ${self.cost_usd}"


class NeedsReviewQueueEntry(models.Model):
    """A proposed change the agent declined to apply on its own.

    The Phase 1 merge policy (see ``apps.agent.pipeline.merge``) writes
    safe fields (filling empty/UNKNOWN slots) but routes anything that
    would overwrite editorial intent — proposed_verdict, launch_status,
    pricing_model, deletions, low-confidence categories — to this queue.
    The Phase 2 admin UI surfaces these for the editor.
    """

    class Kind(models.TextChoices):
        ENRICHED = "enriched", "Enrichment proposal"
        REACTUALIZED = "reactualized", "Re-actualization diff"
        VANISHED = "vanished", "Source vanished"

    class ReviewOutcome(models.TextChoices):
        PENDING = "pending", "Pending review"
        ACCEPTED = "accepted", "Accepted by editor"
        REJECTED = "rejected", "Rejected by editor"
        NO_ACTION = "no_action", "Resolved without applying"
        PUBLISHED = "published", "Approved and published"

    app = models.ForeignKey(
        "catalog.App", on_delete=models.CASCADE,
        related_name="review_queue_entries",
    )
    task = models.ForeignKey(
        EnrichmentTask, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="review_entries",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    payload = models.JSONField(
        help_text=(
            "Structured proposal — keys mirror EnrichedDraft fields. Each"
            " entry typically carries evidence_map and confidence values"
            " so the editor sees WHY the agent proposed the change."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agent_queue_resolutions",
    )
    review_outcome = models.CharField(
        max_length=20,
        choices=ReviewOutcome.choices,
        default=ReviewOutcome.PENDING,
        help_text="Editor decision used for LLM acceptance-rate reporting.",
    )
    resolution_note = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name_plural = "Needs-review queue entries"
        indexes = [
            models.Index(fields=["app", "-created_at"]),
            models.Index(fields=["kind", "resolved_at"]),
            models.Index(
                fields=["review_outcome", "-created_at"],
                name="agent_needs_review__c4bfb5_idx",
            ),
            models.Index(fields=["-created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"ReviewQueue #{self.pk} app={self.app_id} [{self.kind}]"

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None


class BudgetMonthState(models.Model):
    """Per-month snapshot driving the Phase 5 cost hard-stop.

    Beat task ``agent_budget_check`` upserts one row per UTC calendar
    month (keyed by the first-of-month date). Workers consult it
    before every LLM call to decide whether discovery / new agent
    work is allowed; the row is the canonical state, not a cache.

    Two thresholds, both *latching* — set once when the threshold is
    first crossed and only cleared by a manual edit or a budget bump.
    Anti-flap: avoid a "below threshold for an hour, above for the
    next hour" pattern that would auto-reverse the discovery flag and
    let runaway loops sneak budget back.
    """

    month = models.DateField(
        unique=True,
        help_text="First day of the UTC calendar month this row tracks.",
    )
    total_cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, default=0,
        help_text="Sum of LLMCallLog.cost_usd written this month.",
    )
    budget_usd = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Snapshot of AGENT_MONTHLY_BUDGET_USD when the row was last updated.",
    )
    discovery_disabled_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set when monthly cost first crossed 80% of the budget.",
    )
    hard_stop_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set when monthly cost first crossed 100% of the budget.",
    )
    notified_80_at = models.DateTimeField(null=True, blank=True)
    notified_100_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-month"]

    def __str__(self) -> str:
        return f"BudgetMonthState {self.month} ${self.total_cost_usd}/{self.budget_usd}"

    @property
    def is_discovery_disabled(self) -> bool:
        return self.discovery_disabled_at is not None

    @property
    def is_hard_stopped(self) -> bool:
        return self.hard_stop_at is not None
