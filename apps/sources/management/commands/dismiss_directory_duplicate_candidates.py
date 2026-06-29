"""Dismiss weak duplicate candidates caused only by shared directory hosts."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.sources.models import DuplicateCandidate
from apps.sources.upsert import (
    _WEAK_NAME_SIMILARITY_THRESHOLD,
    IGNORED_WEAK_DUPLICATE_DOMAINS,
)


class Command(BaseCommand):
    help = (
        "Dry-run/apply dismissal of weak duplicate candidates whose only "
        "shared domain evidence is a known platform/source directory host."
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
            help="Persist status=dismissed for safe false positives.",
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

        result = dismiss_directory_duplicate_candidates(
            limit=limit,
            apply=options["apply"],
        )
        indent = None if options["indent"] == 0 else options["indent"]
        self.stdout.write(json.dumps(result, indent=indent, sort_keys=True))


def dismiss_directory_duplicate_candidates(
    *,
    limit: int = 100,
    apply: bool = False,
) -> dict:
    decisions = list(_directory_duplicate_decisions(limit=limit))
    if apply:
        now = timezone.now()
        ids = [item["id"] for item in decisions if item["would_dismiss"]]
        if ids:
            DuplicateCandidate.objects.filter(
                pk__in=ids,
                status=DuplicateCandidate.Status.PENDING,
            ).update(
                status=DuplicateCandidate.Status.DISMISSED,
                resolved_at=now,
            )
        for item in decisions:
            item["dismissed"] = item["id"] in ids

    return {
        "apply": apply,
        "evaluated": len(decisions),
        "would_dismiss": sum(item["would_dismiss"] for item in decisions),
        "dismissed": sum(item.get("dismissed", False) for item in decisions),
        "ignored_domains": sorted(IGNORED_WEAK_DUPLICATE_DOMAINS),
        "results": decisions,
    }


def _directory_duplicate_decisions(*, limit: int):
    queryset = (
        DuplicateCandidate.objects.filter(
            status=DuplicateCandidate.Status.PENDING,
            match_reason="shared_domain_similar_name",
        )
        .select_related("app", "candidate_app", "source")
        .order_by("id")
    )
    for duplicate in queryset[:limit]:
        domains = {
            str(domain).lower()
            for domain in (duplicate.evidence or {}).get("domains", [])
            if domain
        }
        directory_only = bool(domains) and domains <= IGNORED_WEAK_DUPLICATE_DOMAINS
        below_name_match = duplicate.score < _WEAK_NAME_SIMILARITY_THRESHOLD
        would_dismiss = directory_only and below_name_match
        yield {
            "id": duplicate.pk,
            "app": duplicate.app.slug,
            "candidate": duplicate.candidate_app.slug,
            "app_name": duplicate.app.name,
            "candidate_name": duplicate.candidate_app.name,
            "source_type": duplicate.source.source_type if duplicate.source else "",
            "score": duplicate.score,
            "domains": sorted(domains),
            "would_dismiss": would_dismiss,
            "dismissed": False,
        }
