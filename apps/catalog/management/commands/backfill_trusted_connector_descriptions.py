"""Backfill short descriptions from trusted connector source payloads."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.trust import (
    TRUSTED_NON_MCP_SOURCE_TYPES,
    trusted_connector_descriptions_backfill,
)
from apps.sources.models import Source


class Command(BaseCommand):
    help = (
        "Dry-run/apply backfill for too-short connector descriptions using "
        "trusted official cloud connector long descriptions."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--source-type",
            action="append",
            default=[],
            choices=(
                Source.SourceType.CLAUDE_CONNECTORS,
                Source.SourceType.CHATGPT_UNOFFICIAL,
            ),
            help=(
                "Trusted source type to include. Repeatable. Defaults to "
                "Claude Connectors and ChatGPT official app URLs."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum apps to evaluate.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist derived short descriptions.",
        )
        parser.add_argument(
            "--include-mcp",
            action="store_true",
            help="Allow apps that also have mcp_registry/MCP taxonomy.",
        )
        parser.add_argument(
            "--indent",
            type=int,
            default=2,
            help="JSON indentation. Use 0 for compact output.",
        )

    def handle(self, *args, **options) -> None:
        limit = options["limit"]
        if limit < 1:
            raise CommandError("--limit must be >= 1")

        source_types = tuple(options["source_type"] or TRUSTED_NON_MCP_SOURCE_TYPES)
        result = trusted_connector_descriptions_backfill(
            source_types=source_types,
            limit=limit,
            apply=options["apply"],
            include_mcp=options["include_mcp"],
        )
        indent = None if options["indent"] == 0 else options["indent"]
        self.stdout.write(json.dumps(result, indent=indent, sort_keys=True))
