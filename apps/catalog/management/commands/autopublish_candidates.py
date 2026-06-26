"""Report and optionally publish conservative autopublish candidates."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.publishing import (
    autopublish_batch,
    normalize_autopublish_source_types,
)
from apps.sources.models import Source


class Command(BaseCommand):
    help = (
        "Evaluate non-MCP draft apps against the conservative autopublish "
        "policy. Dry-run by default; pass --apply for a limited publish pilot."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--source-type",
            action="append",
            default=[],
            help=(
                "Source type to include. Repeatable. Defaults to non-MCP "
                "sources: Gemini, Claude, ChatGPT unofficial."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum draft apps to evaluate.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Publish candidates that pass the policy.",
        )
        parser.add_argument(
            "--no-auto-review",
            action="store_true",
            help="Do not auto-resolve safe enrichment review entries.",
        )
        parser.add_argument(
            "--include-mcp",
            action="store_true",
            help="Allow mcp_registry in --source-type. Disabled by default.",
        )
        parser.add_argument(
            "--indent",
            type=int,
            default=2,
            help="JSON indentation. Use 0 for compact output.",
        )

    def handle(self, *args, **options) -> None:
        try:
            source_types = normalize_autopublish_source_types(options["source_type"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if (
            Source.SourceType.MCP_REGISTRY in source_types
            and not options["include_mcp"]
        ):
            raise CommandError(
                "mcp_registry autopublish requires explicit --include-mcp."
            )

        limit = options["limit"]
        if limit < 1:
            raise CommandError("--limit must be >= 1")

        result = autopublish_batch(
            source_types=source_types,
            limit=limit,
            apply=options["apply"],
            auto_review=not options["no_auto_review"],
        )
        indent = None if options["indent"] == 0 else options["indent"]
        self.stdout.write(json.dumps(result, indent=indent, sort_keys=True))
