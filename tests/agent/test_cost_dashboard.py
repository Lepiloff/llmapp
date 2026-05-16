"""Admin cost-dashboard renderability + aggregate correctness."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.agent.admin import _build_cost_dashboard_context
from apps.agent.models import (
    AgentRun,
    BudgetMonthState,
    EnrichmentTask,
    LLMCallLog,
)


pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_user(db):
    return get_user_model().objects.create_superuser(
        username="admin", email="admin@test.local", password="pw",
    )


@pytest.fixture
def seeded_costs():
    """Seed two runs in the current month with split costs by source."""
    rss = AgentRun.objects.create(
        source_type="rss_discovery",
        status=AgentRun.Status.SUCCEEDED,
        total_cost_usd=Decimal("0.10"),
    )
    gh = AgentRun.objects.create(
        source_type="github_mcp",
        status=AgentRun.Status.SUCCEEDED,
        total_cost_usd=Decimal("0.25"),
    )
    for cost in (Decimal("0.04"), Decimal("0.06")):
        task = EnrichmentTask.objects.create(
            run=rss, status=EnrichmentTask.Status.PERSISTED
        )
        LLMCallLog.objects.create(
            task=task, provider="openai", model="gpt-5.4-mini",
            cost_usd=cost,
        )
    for cost in (Decimal("0.10"), Decimal("0.15")):
        task = EnrichmentTask.objects.create(
            run=gh, status=EnrichmentTask.Status.PERSISTED
        )
        LLMCallLog.objects.create(
            task=task, provider="openai", model="gpt-5.4-nano",
            cost_usd=cost,
        )
    return {"rss": rss, "gh": gh}


@override_settings(AGENT_MONTHLY_BUDGET_USD="10")
def test_dashboard_context_aggregates_correctly(seeded_costs) -> None:
    ctx = _build_cost_dashboard_context()

    assert ctx["month_total"] == Decimal("0.350000")
    assert ctx["budget"] == Decimal("10")
    assert ctx["utilization_pct"] == pytest.approx(3.5)

    by_model = {(r["provider"], r["model"]): r for r in ctx["by_model"]}
    assert by_model[("openai", "gpt-5.4-mini")]["calls"] == 2
    assert by_model[("openai", "gpt-5.4-mini")]["cost"] == Decimal("0.100000")
    assert by_model[("openai", "gpt-5.4-nano")]["cost"] == Decimal("0.250000")

    by_source = {r["task__run__source_type"]: r for r in ctx["by_source"]}
    assert by_source["rss_discovery"]["cost"] == Decimal("0.100000")
    assert by_source["github_mcp"]["cost"] == Decimal("0.250000")

    # Top runs ordered by cost descending — github_mcp first.
    top = list(ctx["top_runs"])
    assert top[0].pk == seeded_costs["gh"].pk
    assert top[1].pk == seeded_costs["rss"].pk


@override_settings(AGENT_MONTHLY_BUDGET_USD="0")
def test_dashboard_context_handles_unconfigured_budget(seeded_costs) -> None:
    ctx = _build_cost_dashboard_context()
    assert ctx["budget"] == Decimal("0")
    assert ctx["utilization_pct"] == 0.0


@override_settings(AGENT_MONTHLY_BUDGET_USD="10")
def test_dashboard_view_renders_for_superuser(admin_user, seeded_costs, client) -> None:
    """End-to-end smoke: superuser → 200 → dashboard sections visible."""
    client.force_login(admin_user)
    response = client.get(reverse("admin:agent_agentrun_cost_dashboard"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Agent cost dashboard" in content
    assert "Current month spend" in content
    assert "$0.3500" in content  # month total formatted
    assert "gpt-5.4-mini" in content
    assert "github_mcp" in content


def test_dashboard_view_requires_admin(client, db) -> None:
    """Anonymous → admin login redirect."""
    response = client.get(reverse("admin:agent_agentrun_cost_dashboard"))
    assert response.status_code in {302, 403}


@override_settings(AGENT_MONTHLY_BUDGET_USD="10")
def test_dashboard_shows_latch_status_when_state_exists(
    admin_user, client,
) -> None:
    """When BudgetMonthState exists the page shows discovery / hard-stop
    pills — the most operationally important signal on the dashboard."""
    from apps.agent.budget import first_of_month
    BudgetMonthState.objects.create(
        month=first_of_month(),
        total_cost_usd=Decimal("8.5"),
        budget_usd=Decimal("10"),
        discovery_disabled_at=timezone.now(),
    )
    client.force_login(admin_user)
    response = client.get(reverse("admin:agent_agentrun_cost_dashboard"))

    content = response.content.decode()
    assert "disabled @" in content
    assert "Manage BudgetMonthState" in content


def test_agentrun_changelist_links_to_dashboard(admin_user, client, db) -> None:
    """The AgentRun list page exposes a Cost dashboard object-tool so
    operators don't need to memorise the URL."""
    client.force_login(admin_user)
    response = client.get(reverse("admin:agent_agentrun_changelist"))
    assert response.status_code == 200
    assert "Cost dashboard" in response.content.decode()
