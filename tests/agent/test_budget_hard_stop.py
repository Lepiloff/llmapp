"""Tests for Phase 5 monthly budget hard-stop.

The end-to-end contract this suite pins:

* ``agent_budget_check`` writes one BudgetMonthState row per month
  and recomputes spend from LLMCallLog every tick.
* At 80% of AGENT_MONTHLY_BUDGET_USD it latches discovery_disabled_at
  exactly once; the orchestrator helpers see discovery as disabled.
* At 100% it latches hard_stop_at; assert_agent_can_run raises and the
  enrichment / re-actualization orchestrators refuse to start.
* Both latches survive subsequent ticks (no auto-unlatch).
* Below threshold no latch is flipped and pending latches from a
  *previous* month don't bleed into the current month (each month
  gets a fresh row).
* Zero/unset budget = no enforcement (Phase 1 dev mode).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from apps.agent.budget import (
    AgentBudgetExceeded,
    assert_agent_can_run,
    configured_budget_usd,
    current_month_cost,
    first_of_month,
    get_current_state,
    is_agent_hard_stopped,
    is_discovery_disabled,
)
from apps.agent.models import (
    AgentRun,
    BudgetMonthState,
    EnrichmentTask,
    LLMCallLog,
)
from apps.agent.tasks import (
    _run_discovery_batch,
    agent_budget_check,
    run_enrich_existing_draft,
    run_reactualize_app,
)


pytestmark = pytest.mark.django_db


def _spent(usd: str) -> LLMCallLog:
    """Record one fake LLMCallLog with the given cost in the current month."""
    run = AgentRun.objects.create(source_type="test", status=AgentRun.Status.SUCCEEDED)
    task = EnrichmentTask.objects.create(run=run, status=EnrichmentTask.Status.PERSISTED)
    return LLMCallLog.objects.create(
        task=task,
        provider="openai",
        model="gpt-mini",
        input_tokens=0,
        output_tokens=0,
        cost_usd=Decimal(usd),
    )


# ---------------------------------------------------------------------------
# configured_budget_usd / current_month_cost
# ---------------------------------------------------------------------------
@override_settings(AGENT_MONTHLY_BUDGET_USD="")
def test_empty_budget_setting_returns_zero() -> None:
    assert configured_budget_usd() == Decimal("0")


@override_settings(AGENT_MONTHLY_BUDGET_USD="20.5")
def test_budget_setting_parsed_as_decimal() -> None:
    assert configured_budget_usd() == Decimal("20.5")


@override_settings(AGENT_MONTHLY_BUDGET_USD="not-a-number")
def test_garbage_budget_setting_falls_back_to_zero() -> None:
    """Defensive — never crash worker bootstrap because of a typo."""
    assert configured_budget_usd() == Decimal("0")


def test_current_month_cost_sums_only_this_month() -> None:
    _spent("0.50")
    _spent("0.30")
    old_log = _spent("99.00")
    # Backdate to previous month (UTC). Use a hard delta past 31 days.
    LLMCallLog.objects.filter(pk=old_log.pk).update(
        created_at=timezone.now() - timedelta(days=45)
    )

    assert current_month_cost() == Decimal("0.800000")


# ---------------------------------------------------------------------------
# agent_budget_check — latches + email
# ---------------------------------------------------------------------------
@override_settings(
    AGENT_MONTHLY_BUDGET_USD="10",
    AGENT_BUDGET_ALERT_EMAILS=["alerts@example.test"],
)
def test_below_threshold_writes_state_but_no_latch_and_no_email() -> None:
    _spent("5")  # 50%

    result = agent_budget_check()

    assert result["utilization"] == pytest.approx(0.5)
    state = get_current_state()
    assert state is not None
    assert state.discovery_disabled_at is None
    assert state.hard_stop_at is None
    assert len(mail.outbox) == 0


@override_settings(
    AGENT_MONTHLY_BUDGET_USD="10",
    AGENT_BUDGET_ALERT_EMAILS=["alerts@example.test"],
)
def test_80_percent_crossing_latches_discovery_and_sends_email_once() -> None:
    _spent("8.50")  # 85%

    first = agent_budget_check()
    second = agent_budget_check()

    state = get_current_state()
    assert state.is_discovery_disabled
    assert not state.is_hard_stopped
    assert first["notified_80_sent"] == 1
    # Second tick must not re-email — the latch is the deduper.
    assert second["notified_80_sent"] == 0
    assert len(mail.outbox) == 1
    assert "80%" in mail.outbox[0].subject
    assert "alerts@example.test" in mail.outbox[0].to


@override_settings(
    AGENT_MONTHLY_BUDGET_USD="10",
    AGENT_BUDGET_ALERT_EMAILS=["alerts@example.test"],
)
def test_100_percent_crossing_latches_hard_stop_and_implies_discovery_off() -> None:
    _spent("12.00")  # 120%

    result = agent_budget_check()

    state = get_current_state()
    assert state.is_hard_stopped
    assert state.is_discovery_disabled  # always implied
    assert result["notified_100_sent"] == 1
    assert "100%" in mail.outbox[0].subject


@override_settings(
    AGENT_MONTHLY_BUDGET_USD="10",
    AGENT_BUDGET_ALERT_EMAILS=[],
    AGENT_REVIEW_DIGEST_EMAILS=[],
    SUBMISSIONS_NOTIFY_EMAILS=[],
)
def test_latch_still_flips_when_no_recipients_configured() -> None:
    """Operator may forget to set the alert recipient. Workers must
    still gate correctly — the latch is the source of truth, the email
    is a courtesy."""
    _spent("12")
    result = agent_budget_check()
    assert get_current_state().is_hard_stopped
    assert result["notified_100_sent"] == 0
    assert len(mail.outbox) == 0


@override_settings(AGENT_MONTHLY_BUDGET_USD="10")
def test_each_month_gets_own_row_so_latches_dont_bleed_across_months() -> None:
    old = BudgetMonthState.objects.create(
        month=(first_of_month() - timedelta(days=31)).replace(day=1),
        total_cost_usd=Decimal("12"),
        budget_usd=Decimal("10"),
        hard_stop_at=timezone.now() - timedelta(days=40),
    )

    _spent("2")  # current month: 20%
    agent_budget_check()

    current = get_current_state()
    assert current.month != old.month
    assert not current.is_hard_stopped


# ---------------------------------------------------------------------------
# Gate functions
# ---------------------------------------------------------------------------
@override_settings(AGENT_MONTHLY_BUDGET_USD="")
def test_no_budget_means_no_enforcement(monkeypatch) -> None:
    BudgetMonthState.objects.create(
        month=first_of_month(),
        total_cost_usd=Decimal("1000"),
        budget_usd=Decimal("0"),
        hard_stop_at=timezone.now(),
    )

    assert is_agent_hard_stopped() is False
    assert is_discovery_disabled() is False


@override_settings(AGENT_MONTHLY_BUDGET_USD="10")
def test_hard_stop_implies_discovery_off() -> None:
    BudgetMonthState.objects.create(
        month=first_of_month(),
        total_cost_usd=Decimal("12"),
        budget_usd=Decimal("10"),
        hard_stop_at=timezone.now(),
    )
    assert is_agent_hard_stopped()
    assert is_discovery_disabled()


@override_settings(AGENT_MONTHLY_BUDGET_USD="10")
def test_discovery_disabled_does_not_hard_stop() -> None:
    BudgetMonthState.objects.create(
        month=first_of_month(),
        total_cost_usd=Decimal("8.5"),
        budget_usd=Decimal("10"),
        discovery_disabled_at=timezone.now(),
    )
    assert is_discovery_disabled()
    assert not is_agent_hard_stopped()


# ---------------------------------------------------------------------------
# Orchestrator refusal
# ---------------------------------------------------------------------------
@override_settings(AGENT_MONTHLY_BUDGET_USD="10")
def test_assert_agent_can_run_raises_when_hard_stopped() -> None:
    BudgetMonthState.objects.create(
        month=first_of_month(),
        total_cost_usd=Decimal("12"),
        budget_usd=Decimal("10"),
        hard_stop_at=timezone.now(),
    )
    with pytest.raises(AgentBudgetExceeded):
        assert_agent_can_run()


@override_settings(AGENT_MONTHLY_BUDGET_USD="10")
def test_enrich_orchestrators_refuse_when_hard_stopped() -> None:
    BudgetMonthState.objects.create(
        month=first_of_month(),
        total_cost_usd=Decimal("12"),
        budget_usd=Decimal("10"),
        hard_stop_at=timezone.now(),
    )

    with pytest.raises(AgentBudgetExceeded):
        run_enrich_existing_draft(app_id=1)
    with pytest.raises(AgentBudgetExceeded):
        run_reactualize_app(app_id=1)
    # The hard-stop fires before the AgentRun row is created, so the
    # audit trail of "what got blocked" lives only in the budget-check
    # task — no orphan run rows.
    assert not AgentRun.objects.filter(source_type="agent_reactualize").exists()


# ---------------------------------------------------------------------------
# Discovery batch gating
# ---------------------------------------------------------------------------
@override_settings(
    AGENT_MONTHLY_BUDGET_USD="10",
    AGENT_SOURCES_ENABLED=["github_mcp"],
)
def test_discovery_batch_skips_when_budget_threshold_reached() -> None:
    BudgetMonthState.objects.create(
        month=first_of_month(),
        total_cost_usd=Decimal("8.5"),
        budget_usd=Decimal("10"),
        discovery_disabled_at=timezone.now(),
    )

    result = _run_discovery_batch(
        source_flag="github_mcp",
        source_label="github_mcp",
        candidates=iter([]),  # would crash if iterated past the gate
        llm=None,
        dry_run=False,
    )

    assert result == {"skipped": "budget_threshold", "source": "github_mcp"}
