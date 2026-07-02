"""Merge exact-name cross-platform duplicate candidates."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.sources.duplicate_merge import merge_cross_platform_duplicate_candidates


class Command(BaseCommand):
    help = (
        "Dry-run/apply merge of exact-name non-MCP duplicate candidates into "
        "one canonical cross-platform App."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum duplicate candidates to evaluate.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Move sources/relations and resolve safe duplicate candidates.",
        )
        parser.add_argument(
            "--include-mcp",
            action="store_true",
            help="Allow MCP source/platform/listing-type duplicates.",
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

        result = merge_cross_platform_duplicate_candidates(
            limit=limit,
            apply=options["apply"],
            include_mcp=options["include_mcp"],
        )
        indent = None if options["indent"] == 0 else options["indent"]
        self.stdout.write(json.dumps(result, indent=indent, sort_keys=True))
