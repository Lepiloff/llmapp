"""Report Phase 3 production-gate evidence."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.agent.reports import phase3_gate_report


class Command(BaseCommand):
    help = (
        "Print Phase 3 -> Phase 4 gate metrics: LLM-generated RSS/GitHub "
        "apps, approval rate, and cost per published app."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON.",
        )

    def handle(self, *args, **options) -> None:
        report = phase3_gate_report()
        payload = report.as_dict()
        if options["json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return

        gate_label = "OPEN" if report.gate_open else "CLOSED"
        cost_per_published = payload["cost_per_published_usd"] or "n/a"
        self.stdout.write("Phase 3 -> Phase 4 gate: " + gate_label)
        self.stdout.write(
            f"Generated RSS/GitHub apps: {report.generated_apps} "
            f"(draft={report.draft_apps}, published={report.published_apps}, "
            f"hidden={report.hidden_apps})"
        )
        self.stdout.write(f"Approval rate: {payload['approval_rate']}%")
        self.stdout.write(
            f"LLM cost: ${payload['total_cost_usd']} total; "
            f"${cost_per_published} per published app"
        )
        self.stdout.write(
            f"LLM calls: {report.llm_calls} "
            f"(real={report.real_llm_calls}, mock={report.mock_llm_calls})"
        )
        self.stdout.write(
            "Cost basis complete: "
            + ("yes" if report.cost_basis_complete else "no")
        )
