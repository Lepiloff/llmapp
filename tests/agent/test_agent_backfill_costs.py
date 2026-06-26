"""Tests for ``manage.py agent_backfill_costs``.

The command exists to repair LLMCallLog rows logged when per-role price
env vars were unset (Phase 3 scale-up shipped before the cost knobs
landed). The behavior we care about:

* Real OpenAI rows with cost_usd=0 get the per-role price applied.
* Mock rows are untouched (they have no real cost).
* Rows whose ``model`` isn't in the configured price table are skipped,
  not zero-filled — operators must explicitly configure each model.
* The command is idempotent.
"""
from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.agent.models import AgentRun, EnrichmentTask, LLMCallLog

pytestmark = pytest.mark.django_db


def _make_run_task() -> EnrichmentTask:
    run = AgentRun.objects.create(source_type="test")
    return EnrichmentTask.objects.create(
        run=run,
        status=EnrichmentTask.Status.PERSISTED,
    )


def _make_log(task, *, model: str, is_mock: bool, **kwargs) -> LLMCallLog:
    defaults = dict(
        provider="openai" if not is_mock else "mock",
        prompt_version="",
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        cost_usd=Decimal("0"),
        latency_ms=0,
    )
    defaults.update(kwargs)
    return LLMCallLog.objects.create(
        task=task, model=model, is_mock=is_mock, **defaults
    )


PRICED_SETTINGS = {
    "AGENT_LLM_PROVIDER_PRIMARY": "openai",
    "AGENT_LLM_MODEL_PRIMARY": "gpt-mini",
    "AGENT_LLM_PROVIDER_CHEAP": "openai",
    "AGENT_LLM_MODEL_CHEAP": "gpt-nano",
    "AGENT_OPENAI_PRIMARY_INPUT_COST_PER_1M_TOKENS": 0.75,
    "AGENT_OPENAI_PRIMARY_CACHED_COST_PER_1M_TOKENS": 0.075,
    "AGENT_OPENAI_PRIMARY_OUTPUT_COST_PER_1M_TOKENS": 4.50,
    "AGENT_OPENAI_CHEAP_INPUT_COST_PER_1M_TOKENS": 0.20,
    "AGENT_OPENAI_CHEAP_CACHED_COST_PER_1M_TOKENS": 0.02,
    "AGENT_OPENAI_CHEAP_OUTPUT_COST_PER_1M_TOKENS": 1.25,
}


@override_settings(**PRICED_SETTINGS)
def test_backfill_applies_per_role_prices_to_zero_rows() -> None:
    task = _make_run_task()
    # 1 primary call: 10k input (2k cached), 1k output → primary prices.
    primary_row = _make_log(
        task, model="gpt-mini", is_mock=False,
        input_tokens=10_000, output_tokens=1_000, cached_tokens=2_000,
    )
    # 1 cheap call: 5k input (0 cached), 200 output → cheap prices.
    cheap_row = _make_log(
        task, model="gpt-nano", is_mock=False,
        input_tokens=5_000, output_tokens=200, cached_tokens=0,
    )
    # 1 mock call: should be untouched.
    mock_row = _make_log(
        task, model="mock-model", is_mock=True,
        input_tokens=1_000, output_tokens=500,
    )

    out = StringIO()
    call_command("agent_backfill_costs", stdout=out)

    primary_row.refresh_from_db()
    cheap_row.refresh_from_db()
    mock_row.refresh_from_db()

    # Primary: (10_000 - 2_000) × 0.75/1M + 2_000 × 0.075/1M + 1_000 × 4.50/1M
    #        = 0.006 + 0.00015 + 0.0045
    #        = 0.01065
    assert primary_row.cost_usd == Decimal("0.010650")
    # Cheap: 5_000 × 0.20/1M + 200 × 1.25/1M
    #      = 0.001 + 0.00025
    #      = 0.00125
    assert cheap_row.cost_usd == Decimal("0.001250")
    # Mock row stays at 0 — backfill never touches mock rows.
    assert mock_row.cost_usd == Decimal("0")


@override_settings(**PRICED_SETTINGS)
def test_backfill_skips_unknown_models_with_warning() -> None:
    task = _make_run_task()
    unknown_row = _make_log(
        task, model="gpt-something-else", is_mock=False,
        input_tokens=1_000, output_tokens=1_000,
    )

    out = StringIO()
    call_command("agent_backfill_costs", stdout=out)

    unknown_row.refresh_from_db()
    assert unknown_row.cost_usd == Decimal("0")  # untouched
    assert "gpt-something-else" in out.getvalue()
    assert "unconfigured model" in out.getvalue()


@override_settings(**PRICED_SETTINGS)
def test_backfill_dry_run_does_not_write() -> None:
    task = _make_run_task()
    row = _make_log(
        task, model="gpt-mini", is_mock=False,
        input_tokens=1_000_000, output_tokens=0, cached_tokens=0,
    )

    call_command("agent_backfill_costs", "--dry-run")

    row.refresh_from_db()
    assert row.cost_usd == Decimal("0")  # nothing written


@override_settings(**PRICED_SETTINGS)
def test_backfill_is_idempotent() -> None:
    task = _make_run_task()
    row = _make_log(
        task, model="gpt-mini", is_mock=False,
        input_tokens=1_000_000, output_tokens=0,
    )

    call_command("agent_backfill_costs")
    first_cost = LLMCallLog.objects.get(pk=row.pk).cost_usd
    # By default --include-nonzero is off, so the second run skips this row.
    call_command("agent_backfill_costs")
    second_cost = LLMCallLog.objects.get(pk=row.pk).cost_usd

    assert first_cost == second_cost == Decimal("0.750000")


@override_settings(**PRICED_SETTINGS)
def test_backfill_include_nonzero_refreshes_existing() -> None:
    """After a price change, --include-nonzero re-applies the new prices
    to rows that already have a (now-stale) cost."""
    task = _make_run_task()
    row = _make_log(
        task, model="gpt-mini", is_mock=False,
        input_tokens=1_000_000, output_tokens=0,
        cost_usd=Decimal("99.999999"),  # stale
    )

    call_command("agent_backfill_costs", "--include-nonzero")

    row.refresh_from_db()
    assert row.cost_usd == Decimal("0.750000")


@override_settings(
    AGENT_LLM_PROVIDER_PRIMARY="mock",
    AGENT_LLM_PROVIDER_CHEAP="mock",
    AGENT_LLM_MODEL_PRIMARY="",
    AGENT_LLM_MODEL_CHEAP="",
)
def test_backfill_noop_when_no_openai_roles_configured() -> None:
    """If neither role uses OpenAI we have no price table; the command
    should report and exit, not crash."""
    task = _make_run_task()
    _make_log(task, model="anything", is_mock=False, input_tokens=1_000)

    out = StringIO()
    call_command("agent_backfill_costs", stdout=out)

    assert "No per-role OpenAI prices configured" in out.getvalue()
