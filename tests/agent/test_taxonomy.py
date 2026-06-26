"""TaxonomySnapshot building from the live ORM."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from apps.agent.persist import build_taxonomy_snapshot
from apps.catalog.models import Capability, Category, ListingType, Platform

pytestmark = pytest.mark.django_db


@pytest.fixture
def reference_data() -> None:
    Platform.objects.get_or_create(
        slug="mcp",
        defaults={"name": "MCP", "public_path": "mcp-servers"},
    )
    Category.objects.get_or_create(
        slug="developer-tools",
        defaults={"name": "Developer Tools"},
    )
    Capability.objects.get_or_create(
        key="open_source",
        defaults={"label": "Open source"},
    )
    ListingType.objects.get_or_create(
        slug="mcp-server",
        defaults={"name": "MCP Server"},
    )


def test_snapshot_reflects_db_state(reference_data) -> None:
    snap = build_taxonomy_snapshot()

    assert "mcp" in snap.platform_slugs
    assert "developer-tools" in snap.category_slugs
    assert "open_source" in snap.capability_keys
    assert "mcp-server" in snap.listing_type_slugs
    assert snap.category_descriptions["developer-tools"] == "Developer Tools"
    assert snap.capability_descriptions["open_source"] == "Open source"


def test_snapshot_is_frozen(reference_data) -> None:
    snap = build_taxonomy_snapshot()
    # Frozen dataclass — assignment raises.
    with pytest.raises(FrozenInstanceError):
        snap.platform_slugs = ("oops",)  # type: ignore[misc]


def test_snapshot_membership_helpers(reference_data) -> None:
    snap = build_taxonomy_snapshot()
    assert snap.has_platform("mcp") is True
    assert snap.has_platform("notreal") is False
    assert snap.has_capability("open_source") is True
    assert snap.has_capability("write_actions") is False
