"""Admin observability and review workflow for the LLM-pipeline agent."""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from django.contrib import admin, messages
from django.contrib.admin.utils import quote
from django.db.models import Count, Sum
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import mark_safe

from apps.agent.budget import (
    configured_budget_usd,
    current_month_cost,
    first_of_month,
    get_current_state,
)
from apps.catalog.models import App
from apps.catalog.services import recalc_quality_score, transition_to_published

from .models import (
    AgentRun,
    BudgetMonthState,
    EnrichmentTask,
    LLMCallLog,
    NeedsReviewQueueEntry,
)


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source_type",
        "status",
        "trigger",
        "started_at",
        "finished_at",
        "total_cost_usd",
        "triggered_by",
    )
    list_filter = ("status", "trigger", "source_type", "started_at")
    search_fields = ("source_type", "triggered_by")
    readonly_fields = (
        "source_type",
        "status",
        "trigger",
        "triggered_by",
        "started_at",
        "finished_at",
        "total_cost_usd",
        "stats",
        "error",
    )
    date_hierarchy = "started_at"

    def has_add_permission(self, request) -> bool:
        return False

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "cost-dashboard/",
                self.admin_site.admin_view(self.cost_dashboard_view),
                name="agent_agentrun_cost_dashboard",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["dashboard_url"] = reverse(
            "admin:agent_agentrun_cost_dashboard"
        )
        return super().changelist_view(request, extra_context=extra_context)

    def cost_dashboard_view(self, request: HttpRequest):
        """One-page aggregate of LLM cost — what BudgetMonthState shows
        plus the per-day / per-model / per-source breakdown an operator
        wants when investigating a spike. Read-only; no actions."""
        context = self.admin_site.each_context(request)
        context.update(_build_cost_dashboard_context())
        return render(
            request, "admin/agent/cost_dashboard.html", context
        )


@admin.register(EnrichmentTask)
class EnrichmentTaskAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "run",
        "app",
        "status",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "started_at")
    search_fields = ("app__name", "app__slug", "source_url")
    readonly_fields = (
        "run",
        "app",
        "source_url",
        "status",
        "started_at",
        "finished_at",
        "error",
        "diff_summary",
    )
    date_hierarchy = "started_at"

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(LLMCallLog)
class LLMCallLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "task",
        "provider",
        "model",
        "prompt_version",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cost_usd",
        "latency_ms",
        "is_mock",
        "created_at",
    )
    list_filter = ("provider", "model", "is_mock", "created_at")
    search_fields = ("provider", "model", "prompt_version")
    readonly_fields = (
        "task",
        "provider",
        "model",
        "prompt_version",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cost_usd",
        "latency_ms",
        "is_mock",
        "created_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request) -> bool:
        return False


# SLA threshold for pending review-queue entries. Default 14 days
# matches the editor-cadence acceptance criteria in
# docs/agent-pipeline.md Phase 1. Operators tighten it via the
# AGENT_REVIEW_QUEUE_SLA_DAYS env var; the constant below is only a
# fallback used when the setting is unset.
_DEFAULT_SLA_PENDING_DAYS = 14


def _sla_pending_days() -> int:
    """Read the configured SLA window at request time so tests can
    override it via ``override_settings``."""
    from django.conf import settings as _settings

    return int(
        getattr(_settings, "AGENT_REVIEW_QUEUE_SLA_DAYS", _DEFAULT_SLA_PENDING_DAYS)
        or _DEFAULT_SLA_PENDING_DAYS
    )


# Back-compat re-export for callers that imported the constant directly
# (and the existing Sprint 2 regression). Kept as the default value, not a
# live setting read — anything wanting the *configured* window must call
# ``_sla_pending_days()``.
SLA_PENDING_DAYS = _DEFAULT_SLA_PENDING_DAYS


@admin.register(NeedsReviewQueueEntry)
class NeedsReviewQueueEntryAdmin(admin.ModelAdmin):
    change_form_template = "admin/agent/needsreviewqueueentry/change_form.html"
    list_display = (
        "id",
        "app",
        "kind",
        "status_badge",
        "review_outcome",
        "created_at",
        "resolved_at",
        "resolved_by",
        "payload_preview",
    )
    list_filter = (
        "kind",
        "review_outcome",
        "resolved_at",
        "created_at",
        "task__run__source_type",
    )
    search_fields = ("app__name", "app__slug", "resolution_note")
    actions = (
        "action_apply_proposed_verdict",
        "action_apply_proposed_launch_status",
        "action_apply_proposed_pricing_model",
        "action_reject_all",
        "action_mark_resolved",
        "action_approve_and_publish",
    )
    readonly_fields = (
        "app",
        "task",
        "kind",
        "current_app_state",
        "proposal_panel",
        "llm_context",
        "payload",
        "created_at",
        "resolved_at",
        "resolved_by",
        "review_outcome",
        "resolution_note",
    )
    fieldsets = (
        (None, {"fields": ("app", "task", "kind", "created_at")}),
        ("Review", {"fields": ("current_app_state", "proposal_panel", "llm_context")}),
        ("Raw payload", {"fields": ("payload",)}),
        (
            "Resolution",
            {"fields": ("resolved_at", "resolved_by", "review_outcome", "resolution_note")},
        ),
    )
    date_hierarchy = "created_at"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "sla-dashboard/",
                self.admin_site.admin_view(self.sla_dashboard_view),
                name="agent_needsreviewqueueentry_sla_dashboard",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["sla_dashboard_url"] = reverse(
            "admin:agent_needsreviewqueueentry_sla_dashboard"
        )
        return super().changelist_view(request, extra_context=extra_context)

    def sla_dashboard_view(self, request: HttpRequest):
        """One-page editor SLA snapshot.

        Reports: oldest pending entry age, count of overdue entries
        (created > SLA_PENDING_DAYS ago and still unresolved),
        breakdown by kind, top-N oldest pending entries with links.
        Editors and oncall folks use this to gauge whether the agent
        backlog is healthy without having to filter the changelist by
        hand. The view is intentionally compact — one short SQL
        round-trip per panel.
        """

        sla_days = _sla_pending_days()
        now = timezone.now()
        sla_cutoff = now - timedelta(days=sla_days)

        pending = NeedsReviewQueueEntry.objects.filter(resolved_at__isnull=True)
        pending_count = pending.count()
        overdue_count = pending.filter(created_at__lt=sla_cutoff).count()

        oldest = pending.order_by("created_at").first()
        oldest_age_days = (
            (now - oldest.created_at).days if oldest else 0
        )

        by_kind = dict(
            pending.values("kind")
            .annotate(c=Count("id"))
            .values_list("kind", "c")
        )

        top_oldest = list(
            pending.select_related("app")
            .order_by("created_at")[:10]
            .values("pk", "app__name", "app__slug", "kind", "created_at")
        )
        for entry in top_oldest:
            entry["age_days"] = (now - entry["created_at"]).days
            entry["overdue"] = entry["created_at"] < sla_cutoff
            entry["url"] = reverse(
                "admin:agent_needsreviewqueueentry_change",
                args=[entry["pk"]],
            )

        context = {
            **self.admin_site.each_context(request),
            "title": "Editor review SLA dashboard",
            "opts": self.model._meta,
            "sla_days": sla_days,
            "pending_count": pending_count,
            "overdue_count": overdue_count,
            "oldest_age_days": oldest_age_days,
            "by_kind": sorted(by_kind.items()),
            "top_oldest": top_oldest,
            "is_healthy": overdue_count == 0,
        }
        return render(
            request,
            "admin/agent/needsreviewqueueentry/sla_dashboard.html",
            context,
        )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("app", "task", "task__run", "resolved_by")
            .prefetch_related("task__llm_calls")
        )

    @admin.display(description="Status")
    def status_badge(self, obj: NeedsReviewQueueEntry) -> str:
        if obj.is_resolved:
            return format_html(
                '<span style="color:{};font-weight:600">resolved</span>',
                "#198754",
            )
        return format_html(
            '<span style="color:{};font-weight:600">open</span>',
            "#b45309",
        )

    def payload_preview(self, obj: NeedsReviewQueueEntry) -> str:
        text = str(obj.payload)
        return format_html("<code>{}</code>", text[:120] + ("…" if len(text) > 120 else ""))

    payload_preview.short_description = "Payload"

    @admin.display(description="Current App")
    def current_app_state(self, obj: NeedsReviewQueueEntry) -> str:
        app = obj.app
        app_url = reverse("admin:catalog_app_change", args=[quote(app.pk)])
        rows = [
            ("App", format_html('<a href="{}">{}</a>', app_url, app.name)),
            ("Status", app.status),
            ("Editorial review", app.editorial_review_status),
            ("Platform verification", app.platform_verification_status),
            ("Verdict", app.verdict or "—"),
            ("Launch status", app.launch_status),
            ("Pricing model", app.pricing_model),
            ("Short description", app.short_description or "—"),
            ("Official page", app.official_page_url or "—"),
            ("Install URL", app.install_url or "—"),
            ("Repo URL", app.repo_url or "—"),
        ]
        return _admin_table(rows)

    @admin.display(description="LLM proposal")
    def proposal_panel(self, obj: NeedsReviewQueueEntry) -> str:
        payload = obj.payload or {}
        rows: list[tuple[str, object]] = []
        for key, label in (
            ("proposed_verdict", "Proposed verdict"),
            ("proposed_launch_status", "Proposed launch status"),
            ("proposed_pricing_model", "Proposed pricing model"),
            ("proposed_scope_summary", "Proposed scope summary"),
            ("rationale", "Rationale"),
        ):
            value = payload.get(key)
            if value:
                rows.append((label, value))

        skipped_fields = payload.get("skipped_field_updates") or []
        skipped_caps = payload.get("skipped_capability_updates") or []
        if skipped_fields:
            rows.append(("Skipped field updates", _json_pretty(skipped_fields)))
        if skipped_caps:
            rows.append(("Skipped capability updates", _json_pretty(skipped_caps)))
        if not rows:
            rows.append(("Proposal", "No pending proposal fields in payload."))
        return _admin_table(rows)

    @admin.display(description="LLM context")
    def llm_context(self, obj: NeedsReviewQueueEntry) -> str:
        call = obj.task.llm_calls.order_by("-created_at").first() if obj.task_id else None
        rows = []
        if obj.task_id:
            task_url = reverse("admin:agent_enrichmenttask_change", args=[quote(obj.task_id)])
            rows.append(("Task", format_html('<a href="{}">#{}</a>', task_url, obj.task_id)))
        if obj.task and obj.task.run_id:
            run_url = reverse("admin:agent_agentrun_change", args=[quote(obj.task.run_id)])
            rows.append(("Run", format_html('<a href="{}">#{}</a>', run_url, obj.task.run_id)))
        if call:
            call_url = reverse("admin:agent_llmcalllog_change", args=[quote(call.pk)])
            rows.extend(
                [
                    ("LLM call", format_html('<a href="{}">#{}</a>', call_url, call.pk)),
                    ("Provider/model", f"{call.provider}/{call.model}"),
                    ("Prompt version", call.prompt_version or "—"),
                    ("Tokens", f"{call.input_tokens} in / {call.output_tokens} out"),
                    ("Cost", f"${call.cost_usd}"),
                    ("Latency", f"{call.latency_ms or 0} ms"),
                ]
            )
        if not rows:
            rows.append(("LLM call", "No linked LLM call."))
        return _admin_table(rows)

    def response_change(self, request: HttpRequest, obj: NeedsReviewQueueEntry):
        button_to_action = {
            "_apply_verdict": self._apply_proposed_verdict,
            "_apply_launch_status": self._apply_proposed_launch_status,
            "_apply_pricing_model": self._apply_proposed_pricing_model,
            "_reject_all": self._reject_entry,
            "_mark_resolved": self._mark_entry_resolved,
            "_approve_publish": self._approve_and_publish_entry,
        }
        for button, handler in button_to_action.items():
            if button in request.POST:
                handler(request, obj)
                return HttpResponseRedirect(".")
        return super().response_change(request, obj)

    @admin.action(description="Apply proposed verdict")
    def action_apply_proposed_verdict(self, request, queryset):
        count = sum(self._apply_proposed_verdict(request, entry, quiet=True) for entry in queryset)
        self.message_user(request, f"Applied proposed verdict to {count} entry(s).")

    @admin.action(description="Apply proposed launch_status")
    def action_apply_proposed_launch_status(self, request, queryset):
        count = sum(
            self._apply_proposed_launch_status(request, entry, quiet=True)
            for entry in queryset
        )
        self.message_user(request, f"Applied proposed launch_status to {count} entry(s).")

    @admin.action(description="Apply proposed pricing_model")
    def action_apply_proposed_pricing_model(self, request, queryset):
        count = sum(
            self._apply_proposed_pricing_model(request, entry, quiet=True)
            for entry in queryset
        )
        self.message_user(request, f"Applied proposed pricing_model to {count} entry(s).")

    @admin.action(description="Reject all proposals and mark resolved")
    def action_reject_all(self, request, queryset):
        count = sum(self._reject_entry(request, entry, quiet=True) for entry in queryset)
        self.message_user(request, f"Rejected {count} entry(s).")

    @admin.action(description="Mark resolved without applying")
    def action_mark_resolved(self, request, queryset):
        count = sum(self._mark_entry_resolved(request, entry, quiet=True) for entry in queryset)
        self.message_user(request, f"Marked {count} entry(s) resolved.")

    @admin.action(description="Approve App publish gate and mark queue resolved")
    def action_approve_and_publish(self, request, queryset):
        succeeded = 0
        failures: list[str] = []
        for entry in queryset.select_related("app"):
            try:
                if self._approve_and_publish_entry(request, entry, quiet=True):
                    succeeded += 1
            except ValueError as exc:
                failures.append(f"{entry.app.name}: {exc}")
        if failures:
            self.message_user(request, "; ".join(failures), level=messages.WARNING)
        self.message_user(request, f"Published {succeeded} app(s).")

    def _apply_proposed_verdict(
        self, request, entry: NeedsReviewQueueEntry, *, quiet: bool = False
    ) -> bool:
        if entry.is_resolved:
            return self._skip(request, quiet, "Entry is already resolved.")
        verdict = (entry.payload or {}).get("proposed_verdict", "").strip()
        if not verdict:
            return self._skip(request, quiet, "No proposed verdict in this entry.")
        App.objects.filter(pk=entry.app_id).update(verdict=verdict)
        self._record_outcome(entry, NeedsReviewQueueEntry.ReviewOutcome.ACCEPTED)
        self._refresh_and_recalc(entry)
        return self._ok(request, quiet, "Applied proposed verdict.")

    def _apply_proposed_launch_status(
        self, request, entry: NeedsReviewQueueEntry, *, quiet: bool = False
    ) -> bool:
        if entry.is_resolved:
            return self._skip(request, quiet, "Entry is already resolved.")
        value = (entry.payload or {}).get("proposed_launch_status")
        if value not in App.LaunchStatus.values:
            return self._skip(request, quiet, "No valid proposed launch_status.")
        App.objects.filter(pk=entry.app_id).update(launch_status=value)
        self._record_outcome(entry, NeedsReviewQueueEntry.ReviewOutcome.ACCEPTED)
        self._refresh_and_recalc(entry)
        return self._ok(request, quiet, "Applied proposed launch_status.")

    def _apply_proposed_pricing_model(
        self, request, entry: NeedsReviewQueueEntry, *, quiet: bool = False
    ) -> bool:
        if entry.is_resolved:
            return self._skip(request, quiet, "Entry is already resolved.")
        value = (entry.payload or {}).get("proposed_pricing_model")
        if value not in App.PricingModel.values:
            return self._skip(request, quiet, "No valid proposed pricing_model.")
        App.objects.filter(pk=entry.app_id).update(pricing_model=value)
        self._record_outcome(entry, NeedsReviewQueueEntry.ReviewOutcome.ACCEPTED)
        self._refresh_and_recalc(entry)
        return self._ok(request, quiet, "Applied proposed pricing_model.")

    def _reject_entry(
        self, request, entry: NeedsReviewQueueEntry, *, quiet: bool = False
    ) -> bool:
        return self._resolve(
            request,
            entry,
            note="Rejected all LLM proposals",
            outcome=NeedsReviewQueueEntry.ReviewOutcome.REJECTED,
            quiet=quiet,
            message="Rejected all proposals and marked resolved.",
        )

    def _mark_entry_resolved(
        self, request, entry: NeedsReviewQueueEntry, *, quiet: bool = False
    ) -> bool:
        return self._resolve(
            request,
            entry,
            note="Marked resolved by editor",
            outcome=NeedsReviewQueueEntry.ReviewOutcome.NO_ACTION,
            quiet=quiet,
            message="Marked resolved.",
        )

    def _approve_and_publish_entry(
        self, request, entry: NeedsReviewQueueEntry, *, quiet: bool = False
    ) -> bool:
        if entry.is_resolved:
            return self._skip(request, quiet, "Entry is already resolved.")
        transition_to_published(entry.app, request.user)
        self._resolve(
            request,
            entry,
            note="Approved and published by editor",
            outcome=NeedsReviewQueueEntry.ReviewOutcome.PUBLISHED,
            quiet=True,
            message="",
        )
        return self._ok(request, quiet, "Published app and marked queue entry resolved.")

    def _resolve(
        self,
        request,
        entry: NeedsReviewQueueEntry,
        *,
        note: str,
        outcome: str,
        quiet: bool,
        message: str,
    ) -> bool:
        if entry.is_resolved:
            return self._skip(request, quiet, "Entry is already resolved.")
        entry.resolved_at = timezone.now()
        entry.resolved_by = request.user
        entry.review_outcome = outcome
        entry.resolution_note = note
        entry.save(
            update_fields=[
                "resolved_at",
                "resolved_by",
                "review_outcome",
                "resolution_note",
            ]
        )
        return self._ok(request, quiet, message)

    def _record_outcome(self, entry: NeedsReviewQueueEntry, outcome: str) -> None:
        entry.review_outcome = outcome
        entry.save(update_fields=["review_outcome"])

    def _ok(self, request, quiet: bool, message: str) -> bool:
        if not quiet and message:
            self.message_user(request, message)
        return True

    def _skip(self, request, quiet: bool, message: str) -> bool:
        if not quiet:
            self.message_user(request, message, level=messages.WARNING)
        return False

    def _refresh_and_recalc(self, entry: NeedsReviewQueueEntry) -> None:
        entry.app.refresh_from_db()
        recalc_quality_score(entry.app)

    def has_add_permission(self, request) -> bool:
        return False


def _admin_table(rows: list[tuple[str, object]]) -> str:
    return format_html(
        '<table style="width:100%;border-collapse:collapse">{}</table>',
        format_html_join(
            "",
            (
                '<tr><th style="width:220px;text-align:left;vertical-align:top;'
                'border-bottom:1px solid #eee;padding:6px 8px">{}</th>'
                '<td style="border-bottom:1px solid #eee;padding:6px 8px">{}</td></tr>'
            ),
            rows,
        ),
    )


def _json_pretty(value) -> str:
    return mark_safe(
        "<pre style='white-space:pre-wrap;margin:0'>"
        + escape(json.dumps(value, indent=2, sort_keys=True))
        + "</pre>"
    )


@admin.register(BudgetMonthState)
class BudgetMonthStateAdmin(admin.ModelAdmin):
    """Visible so operators can clear the discovery/hard-stop latches.

    The beat task ``agent_budget_check`` writes this row hourly; both
    timestamp fields latch once set within a month. To resume agent
    work after a budget breach, either bump
    ``AGENT_MONTHLY_BUDGET_USD`` (the next beat tick clears both
    flags) or clear the timestamps here directly.
    """

    list_display = (
        "month", "utilization_pct", "total_cost_usd", "budget_usd",
        "is_discovery_disabled", "is_hard_stopped", "updated_at",
    )
    readonly_fields = ("total_cost_usd", "budget_usd", "updated_at")
    fields = (
        "month", "total_cost_usd", "budget_usd", "updated_at",
        "discovery_disabled_at", "hard_stop_at",
        "notified_80_at", "notified_100_at",
    )
    ordering = ("-month",)

    @admin.display(description="Utilization", ordering="total_cost_usd")
    def utilization_pct(self, obj: BudgetMonthState) -> str:
        if not obj.budget_usd:
            return "—"
        return f"{float(obj.total_cost_usd / obj.budget_usd) * 100:.1f}%"

    @admin.display(boolean=True, description="Discovery off")
    def is_discovery_disabled(self, obj: BudgetMonthState) -> bool:
        return obj.is_discovery_disabled

    @admin.display(boolean=True, description="Hard stop")
    def is_hard_stopped(self, obj: BudgetMonthState) -> bool:
        return obj.is_hard_stopped


# ---------------------------------------------------------------------------
# Cost dashboard helpers
# ---------------------------------------------------------------------------
def _build_cost_dashboard_context() -> dict:
    """Aggregate context for the admin cost dashboard view.

    Covers the three views an operator wants when investigating spend:
    current-month budget snapshot, per-day cost trend (last 30 days),
    and per-source × per-model breakdown for the current month. Top
    expensive AgentRuns this month round out the page so a spike is
    one click from its root cause.
    """
    now = timezone.now()
    month_start = first_of_month(now)
    budget = configured_budget_usd()
    month_total = current_month_cost()
    state = get_current_state()

    last_30 = now - timedelta(days=30)
    by_day_qs = (
        LLMCallLog.objects.filter(
            created_at__gte=last_30, is_mock=False
        )
        .extra(select={"day": "date(created_at)"})  # noqa: SLF001 — admin dashboard
        .values("day")
        .annotate(cost=Sum("cost_usd"), calls=Count("id"))
        .order_by("-day")
    )
    by_day = [
        {"day": row["day"], "cost": row["cost"] or Decimal("0"), "calls": row["calls"]}
        for row in by_day_qs
    ]

    by_model = (
        LLMCallLog.objects.filter(
            created_at__date__gte=month_start, is_mock=False
        )
        .values("provider", "model")
        .annotate(cost=Sum("cost_usd"), calls=Count("id"))
        .order_by("-cost")
    )

    by_source = (
        LLMCallLog.objects.filter(
            created_at__date__gte=month_start, is_mock=False
        )
        .values("task__run__source_type")
        .annotate(cost=Sum("cost_usd"), calls=Count("id"))
        .order_by("-cost")
    )

    top_runs = (
        AgentRun.objects.filter(started_at__date__gte=month_start)
        .order_by("-total_cost_usd")[:10]
    )

    utilization_pct = float(month_total / budget) * 100 if budget else 0.0

    return {
        "title": "Agent cost dashboard",
        "month_start": month_start,
        "month_total": month_total,
        "budget": budget,
        "utilization_pct": utilization_pct,
        "state": state,
        "by_day": by_day,
        "by_model": by_model,
        "by_source": by_source,
        "top_runs": top_runs,
        "budget_state_changelist_url": reverse(
            "admin:agent_budgetmonthstate_changelist"
        ),
    }
