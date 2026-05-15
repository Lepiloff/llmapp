"""Recompute ``LLMCallLog.cost_usd`` for rows logged before per-role pricing.

Phase 3 runs persisted ``cost_usd=0`` because the cost env vars were
unset at the time. This command walks every non-mock row, matches the
row's ``model`` slug against the currently configured primary/cheap
models, and writes the recomputed cost in place.

Rows whose ``model`` doesn't match either configured role are skipped
with a warning: we have no price for them, so writing zero would be
indistinguishable from "already zero" and writing a guess would be
worse than leaving the gap visible.

Idempotent: re-running with the same prices yields the same numbers,
so it's safe to re-run after a price change to refresh older rows.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.agent.llm.client import _estimate_cost_usd
from apps.agent.models import LLMCallLog


class Command(BaseCommand):
    help = "Recompute LLMCallLog.cost_usd using current per-role OpenAI prices."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )
        parser.add_argument(
            "--include-nonzero",
            action="store_true",
            help=(
                "Also recompute rows whose cost_usd is already > 0 "
                "(use after a price change). Default: only fill rows "
                "currently at 0."
            ),
        )

    def handle(self, *args, **opts) -> None:
        price_table = _build_price_table()
        if not price_table:
            self.stdout.write(self.style.WARNING(
                "No per-role OpenAI prices configured. Set "
                "AGENT_OPENAI_PRIMARY_*_COST_PER_1M_TOKENS and "
                "AGENT_OPENAI_CHEAP_*_COST_PER_1M_TOKENS first."
            ))
            return

        self.stdout.write(self.style.NOTICE("Configured prices:"))
        for model_slug, prices in price_table.items():
            self.stdout.write(
                f"  {model_slug}: "
                f"input ${prices['input']:.4f} / "
                f"cached ${prices['cached']:.4f} / "
                f"output ${prices['output']:.4f} per 1M tokens"
            )

        qs = LLMCallLog.objects.filter(is_mock=False)
        if not opts["include_nonzero"]:
            qs = qs.filter(cost_usd=0)

        updated = 0
        skipped_unknown_model: dict[str, int] = {}
        total_cost_before = Decimal("0")
        total_cost_after = Decimal("0")

        with transaction.atomic():
            for row in qs.select_for_update():
                prices = price_table.get(row.model)
                if prices is None:
                    skipped_unknown_model[row.model] = (
                        skipped_unknown_model.get(row.model, 0) + 1
                    )
                    continue
                new_cost = Decimal(str(_estimate_cost_usd(
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                    cached_tokens=row.cached_tokens,
                    input_cost_per_1m_tokens=prices["input"],
                    output_cost_per_1m_tokens=prices["output"],
                    cached_input_cost_per_1m_tokens=prices["cached"],
                ))).quantize(Decimal("0.000001"))

                total_cost_before += row.cost_usd
                total_cost_after += new_cost

                if new_cost != row.cost_usd:
                    if not opts["dry_run"]:
                        LLMCallLog.objects.filter(pk=row.pk).update(
                            cost_usd=new_cost
                        )
                    updated += 1

        verb = "Would update" if opts["dry_run"] else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {updated} LLMCallLog row(s). "
            f"Total cost before: ${total_cost_before:.6f}; "
            f"after: ${total_cost_after:.6f}."
        ))
        for model_slug, count in skipped_unknown_model.items():
            self.stdout.write(self.style.WARNING(
                f"Skipped {count} row(s) with unconfigured model "
                f"{model_slug!r} — no price in role table."
            ))


def _build_price_table() -> dict[str, dict[str, float]]:
    """Return ``{model_slug: {"input": x, "cached": y, "output": z}}``.

    Only includes roles whose provider is OpenAI; mock and Anthropic
    rows are not OpenAI-priced. Both roles may target the same model
    (e.g. primary=cheap=gpt-5.4-mini); in that case primary's prices
    win, which matches the production-call side: ``build_provider``
    always uses primary-role prices for the primary role.
    """
    table: dict[str, dict[str, float]] = {}
    for role, prefix in (("cheap", "AGENT_OPENAI_CHEAP"),
                        ("primary", "AGENT_OPENAI_PRIMARY")):
        provider_key = getattr(settings, f"AGENT_LLM_PROVIDER_{role.upper()}", "")
        if (provider_key or "").lower() != "openai":
            continue
        model = getattr(settings, f"AGENT_LLM_MODEL_{role.upper()}", "")
        if not model:
            continue
        table[model] = {
            "input": float(getattr(settings, f"{prefix}_INPUT_COST_PER_1M_TOKENS", 0) or 0),
            "cached": float(getattr(settings, f"{prefix}_CACHED_COST_PER_1M_TOKENS", 0) or 0),
            "output": float(getattr(settings, f"{prefix}_OUTPUT_COST_PER_1M_TOKENS", 0) or 0),
        }
    return table
