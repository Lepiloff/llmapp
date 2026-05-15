"""Phase 5 cost hard-stop helpers.

Two responsibilities:

1. Compute the canonical *current month* budget snapshot from
   ``LLMCallLog`` + the configured ``AGENT_MONTHLY_BUDGET_USD``.
2. Provide ``is_discovery_disabled()`` / ``is_agent_hard_stopped()``
   gates that orchestrator code can call before any new LLM work.

The persisted state lives in ``BudgetMonthState``; this module owns
the read/upsert logic so the beat task and the runtime gates share
exactly one source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.agent.models import BudgetMonthState, LLMCallLog


def first_of_month(when=None) -> date:
    """Return the first UTC day of the month for ``when`` (default: now)."""
    now = when or timezone.now()
    return now.date().replace(day=1)


def configured_budget_usd() -> Decimal:
    """Parse ``AGENT_MONTHLY_BUDGET_USD`` into a ``Decimal``.

    The settings key is ``cast=str`` (empty == not configured) to let
    Phase 1 dev environments boot without a budget. An unset budget
    returns ``Decimal("0")`` here, which the gates treat as "no
    enforcement" — see :func:`is_agent_hard_stopped`.
    """
    raw = getattr(settings, "AGENT_MONTHLY_BUDGET_USD", "")
    if raw in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal("0")


@dataclass
class BudgetSnapshot:
    """Read-side view returned by :func:`get_or_create_current_state`."""

    month: date
    total_cost_usd: Decimal
    budget_usd: Decimal
    discovery_disabled: bool
    hard_stopped: bool

    @property
    def utilization(self) -> float:
        if self.budget_usd <= 0:
            return 0.0
        return float(self.total_cost_usd / self.budget_usd)


def current_month_cost() -> Decimal:
    """Sum every non-zero ``LLMCallLog.cost_usd`` written this UTC month."""
    month_start = first_of_month()
    total = LLMCallLog.objects.filter(
        created_at__date__gte=month_start
    ).aggregate(s=Sum("cost_usd"))["s"]
    return Decimal(total) if total is not None else Decimal("0")


def get_current_state() -> BudgetMonthState | None:
    """Return the BudgetMonthState for the current UTC month, if any."""
    return BudgetMonthState.objects.filter(month=first_of_month()).first()


def is_agent_hard_stopped() -> bool:
    """True if *any* new agent LLM work should refuse to run.

    Zero / unset budget = no enforcement. This is the gate that
    enrichment, re-actualization, and discovery all consult.
    """
    if configured_budget_usd() <= 0:
        return False
    state = get_current_state()
    return bool(state and state.is_hard_stopped)


def is_discovery_disabled() -> bool:
    """True if discovery beat tasks should refuse to run.

    Strictly weaker than the hard stop: hard stop disables everything
    (including re-actualization); the 80% threshold disables only
    discovery so we keep re-actualizing the catalog we already paid
    for. Operators expect "spend the budget on freshness before
    spending it on net-new cards" semantics.
    """
    if configured_budget_usd() <= 0:
        return False
    state = get_current_state()
    if state is None:
        return False
    return state.is_discovery_disabled or state.is_hard_stopped


class AgentBudgetExceeded(RuntimeError):
    """Raised by orchestrator gates when the hard-stop is active.

    Distinct from generic RuntimeError so callers (admin actions, the
    enrich-app management command, batch loops) can render a clean
    message without leaking pricing internals.
    """


def assert_agent_can_run() -> None:
    """Raise :class:`AgentBudgetExceeded` when the hard-stop is active.

    Called inside orchestrator entry points (run_enrich_existing_draft,
    run_enrich_new_app, run_reactualize_app) BEFORE any LLM call. The
    AgentRun / EnrichmentTask audit rows are not yet created — failing
    early keeps the audit trail of "what got blocked" cleanly visible
    only on the beat task that flipped the flag.
    """
    if is_agent_hard_stopped():
        raise AgentBudgetExceeded(
            "Monthly agent budget exhausted — manual review required "
            "(see admin BudgetMonthState)."
        )
