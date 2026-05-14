from __future__ import annotations

import pytest

from django.utils import timezone

from apps.catalog.models import App
from apps.sources.models import Source


pytestmark = pytest.mark.django_db


def test_source_tracks_last_enriched_at_independently() -> None:
    app = App.objects.create(
        name="Source Test",
        slug="source-test",
        short_description="Source metadata test.",
        status=App.AppStatus.DRAFT,
    )
    fetched_at = timezone.now()
    source = Source.objects.create(
        app=app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="mcp-registry:source-test",
        fetched_at=fetched_at,
    )

    assert source.last_enriched_at is None

    enriched_at = timezone.now()
    Source.objects.filter(pk=source.pk).update(last_enriched_at=enriched_at)
    source.refresh_from_db()

    assert source.fetched_at == fetched_at
    assert source.last_enriched_at == enriched_at
