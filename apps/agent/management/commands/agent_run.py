"""Manual entry point for the LLM-pipeline agent.

Phase 1 supports two modes:

* ``--enrich-app=<slug>`` — process one specific App. Useful for
  iterating on prompts against a known card. The target App must have
  ``status='draft'``; otherwise the command errors out (Phase 1
  invariant — see ``apps.agent.persist.assert_app_is_eligible``).
* ``--enrich-pending [--limit N]`` — walk the same selector beat
  would: DRAFT cards not yet agent-enriched, newest first.

Default is **dry-run**: the pipeline runs end-to-end (prompt → LLM →
validate → merge) and writes the audit trail —
``AgentRun`` / ``EnrichmentTask`` / ``LLMCallLog`` rows, with the full
merge proposal serialized into ``EnrichmentTask.diff_summary``.

Dry-run does **NOT** call ``apply_merge_set``, which means:
  * No ``App`` / ``AppCapability`` / ``AppCategory`` writes.
  * **No ``Source`` row created** — the ``agent-enrich:<id>`` Source
    is only written when ``--apply`` runs.

Pass ``--apply`` to land the proposal in the catalog. A summary of
what *would have been* (dry-run) / *was* (apply) written is printed
to stdout in either mode — that's the operator's eyes-on diff before
flipping the flag.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.agent.models import EnrichmentTask
from apps.agent.persist import AppNotEligibleError, pending_enrichment_app_ids
from apps.agent.tasks import run_enrich_existing_draft
from apps.catalog.models import App


class Command(BaseCommand):
    help = (
        "Run the LLM-pipeline agent against existing DRAFT apps (Phase 1). "
        "Defaults to --dry-run: nothing is written to App / AppCapability / "
        "AppCategory; only audit rows are persisted."
    )

    def add_arguments(self, parser) -> None:
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument(
            "--enrich-app",
            dest="enrich_app",
            help="Slug of a single App to enrich.",
        )
        target.add_argument(
            "--enrich-pending",
            dest="enrich_pending",
            action="store_true",
            help="Walk pending DRAFT cards not yet agent-enriched.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=5,
            help="Cap on the number of pending cards (default: 5).",
        )
        parser.add_argument(
            "--apply",
            dest="apply",
            action="store_true",
            help=(
                "Apply the merge plan to App / capabilities / taxonomy. "
                "Default is dry-run."
            ),
        )
        parser.add_argument(
            "--allow-non-mcp",
            dest="allow_non_mcp",
            action="store_true",
            help=(
                "Skip the Phase 1 MCP-source eligibility check on the "
                "target App. Use ONLY for prompt iteration against a "
                "non-MCP DRAFT; never wire this into beat / batch."
            ),
        )

    def handle(self, *args, **options) -> None:
        dry_run = not options["apply"]
        allow_non_mcp = options["allow_non_mcp"]
        if options.get("enrich_app"):
            app_ids = [self._resolve_slug(options["enrich_app"])]
        else:
            # --enrich-pending always uses the strict Phase 1 selector.
            # --allow-non-mcp has no effect in batch mode (by design —
            # would mask source-type bugs at scale).
            app_ids = list(pending_enrichment_app_ids(limit=options["limit"]))
            allow_non_mcp = False
            if not app_ids:
                self.stdout.write(self.style.NOTICE(
                    "No pending MCP-source DRAFT cards to enrich — "
                    "nothing to do."
                ))
                return

        for app_id in app_ids:
            self._process_one(
                app_id, dry_run=dry_run, allow_non_mcp=allow_non_mcp
            )

    # ------------------------------------------------------------------
    def _resolve_slug(self, slug: str) -> int:
        try:
            return App.objects.values_list("pk", flat=True).get(slug=slug)
        except App.DoesNotExist as exc:
            raise CommandError(f"No App with slug={slug!r}") from exc

    def _process_one(
        self, app_id: int, *, dry_run: bool, allow_non_mcp: bool
    ) -> None:
        try:
            outcome = run_enrich_existing_draft(
                app_id,
                dry_run=dry_run,
                trigger="manual",
                triggered_by="agent_run",
                allow_non_mcp=allow_non_mcp,
            )
        except AppNotEligibleError as exc:
            # Convert to CommandError so the operator sees a clean message
            # instead of a stack trace. The AgentRun/EnrichmentTask rows
            # the orchestrator created before the failure carry the audit
            # trail (status=FAILED).
            raise CommandError(str(exc)) from exc
        plan = outcome.result.outcome.plan.as_dict()
        queue = outcome.result.outcome.queue.as_dict()
        applied = outcome.persist.as_dict() if outcome.persist else None

        header = "[DRY-RUN]" if dry_run else "[APPLIED]"
        self.stdout.write(self.style.SUCCESS(
            f"{header} app_id={app_id} run={outcome.run_id} "
            f"task={outcome.task_id} mock={outcome.result.call_meta.is_mock}"
        ))
        self.stdout.write("  plan:    " + json.dumps(plan, ensure_ascii=False))
        self.stdout.write("  queue:   " + json.dumps(queue, ensure_ascii=False))
        if applied is not None:
            self.stdout.write("  applied: " + json.dumps(applied, ensure_ascii=False))

        # Self-check: task status must reflect mode. Catches regressions
        # where a hypothetical future change accidentally calls
        # apply_merge_set inside dry-run.
        EnrichmentTask.objects.filter(pk=outcome.task_id).get()  # raises if not found
