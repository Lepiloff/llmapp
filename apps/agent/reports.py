"""Operational reports for agent rollout gates."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, Q, Sum

from apps.agent.models import EnrichmentTask, LLMCallLog
from apps.catalog.models import App
from apps.sources.models import Source

PHASE3_SOURCE_TYPES = (
    Source.SourceType.RSS_DISCOVERY,
    Source.SourceType.GITHUB_MCP,
)

PHASE3_MIN_GENERATED_APPS = 20
PHASE3_MIN_APPROVAL_RATE = Decimal("50.0")


@dataclass(frozen=True)
class Phase3GateReport:
    """Rollup for the Phase 3 -> Phase 4 production gate."""

    generated_apps: int
    draft_apps: int
    published_apps: int
    hidden_apps: int
    other_status_apps: int
    llm_calls: int
    mock_llm_calls: int
    real_llm_calls: int
    total_cost_usd: Decimal
    approval_rate: Decimal
    cost_per_published_usd: Decimal | None
    cost_basis_complete: bool
    gate_open: bool

    def as_dict(self) -> dict:
        return {
            "generated_apps": self.generated_apps,
            "draft_apps": self.draft_apps,
            "published_apps": self.published_apps,
            "hidden_apps": self.hidden_apps,
            "other_status_apps": self.other_status_apps,
            "llm_calls": self.llm_calls,
            "mock_llm_calls": self.mock_llm_calls,
            "real_llm_calls": self.real_llm_calls,
            "total_cost_usd": _money(self.total_cost_usd),
            "approval_rate": _rate(self.approval_rate),
            "cost_per_published_usd": (
                None
                if self.cost_per_published_usd is None
                else _money(self.cost_per_published_usd)
            ),
            "cost_basis_complete": self.cost_basis_complete,
            "gate_open": self.gate_open,
            "gate_thresholds": {
                "generated_apps_min": PHASE3_MIN_GENERATED_APPS,
                "approval_rate_min": _rate(PHASE3_MIN_APPROVAL_RATE),
            },
        }


def phase3_gate_report() -> Phase3GateReport:
    """Return measured evidence for the Phase 3 production gate.

    Source rows with ``payload.agent_enrichment`` are the source of truth:
    those rows are only written after new-app LLM enrichment has produced a
    sanitized draft and ``persist_new_draft`` has created or updated an App.
    """

    generated_source_qs = Source.objects.filter(
        source_type__in=PHASE3_SOURCE_TYPES,
        payload__has_key="agent_enrichment",
    )
    app_ids = generated_source_qs.values_list("app_id", flat=True).distinct()

    status_counts = {
        row["status"]: row["count"]
        for row in App.objects.filter(pk__in=app_ids)
        .values("status")
        .annotate(count=Count("id"))
    }
    generated_apps = sum(status_counts.values())
    draft_apps = status_counts.get(App.AppStatus.DRAFT, 0)
    published_apps = status_counts.get(App.AppStatus.PUBLISHED, 0)
    hidden_apps = status_counts.get(App.AppStatus.HIDDEN, 0)
    other_status_apps = generated_apps - draft_apps - published_apps - hidden_apps

    llm_calls_qs = LLMCallLog.objects.filter(
        task__status=EnrichmentTask.Status.PERSISTED,
        task__app_id__in=app_ids,
        task__run__source_type__in=PHASE3_SOURCE_TYPES,
        prompt_version="enrich-new-v1.0",
    ).distinct()
    llm_call_counts = llm_calls_qs.aggregate(
        total=Count("id"),
        mock=Count("id", filter=Q(is_mock=True)),
        cost=Sum("cost_usd"),
    )
    total_cost = llm_call_counts["cost"] or Decimal("0")
    approval_rate = _percent(published_apps, generated_apps)
    cost_per_published = (
        (total_cost / Decimal(published_apps))
        if published_apps
        else None
    )
    llm_calls = llm_call_counts["total"] or 0
    mock_llm_calls = llm_call_counts["mock"] or 0
    cost_basis_complete = (
        generated_apps > 0
        and llm_calls >= generated_apps
        and mock_llm_calls == 0
    )
    gate_open = (
        generated_apps >= PHASE3_MIN_GENERATED_APPS
        and approval_rate >= PHASE3_MIN_APPROVAL_RATE
        and cost_per_published is not None
        and cost_basis_complete
    )

    return Phase3GateReport(
        generated_apps=generated_apps,
        draft_apps=draft_apps,
        published_apps=published_apps,
        hidden_apps=hidden_apps,
        other_status_apps=other_status_apps,
        llm_calls=llm_calls,
        mock_llm_calls=mock_llm_calls,
        real_llm_calls=llm_calls - mock_llm_calls,
        total_cost_usd=total_cost,
        approval_rate=approval_rate,
        cost_per_published_usd=cost_per_published,
        cost_basis_complete=cost_basis_complete,
        gate_open=gate_open,
    )


def _percent(part: int, total: int) -> Decimal:
    if total <= 0:
        return Decimal("0.0")
    return (Decimal(part) * Decimal("100") / Decimal(total)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _rate(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
