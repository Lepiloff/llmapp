"""Remove false MCP taxonomy from trusted cloud connectors."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.trust import (
    TRUSTED_NON_MCP_SOURCE_TYPES,
    trusted_connector_mcp_taxonomy_repair,
)
from apps.sources.models import Source


class Command(BaseCommand):
    help = (
        "Dry-run/apply repair for trusted cloud connectors that have an "
        "mcp-server listing type but no MCP source/platform."
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
            help="Remove false MCP listing types.",
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
        result = trusted_connector_mcp_taxonomy_repair(
            source_types=source_types,
            limit=limit,
            apply=options["apply"],
        )
        indent = None if options["indent"] == 0 else options["indent"]
        self.stdout.write(json.dumps(result, indent=indent, sort_keys=True))
