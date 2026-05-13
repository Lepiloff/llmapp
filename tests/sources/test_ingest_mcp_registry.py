"""Phase 0 regression tests for the rewritten MCP Registry ingest.

Confirms the Phase 0 acceptance criteria from ``docs/agent-pipeline.md``:

* Creates ``App`` + ``AppPlatform`` + ``Source`` via ``upsert_app_from_draft``.
* Apps are marked ``platform_verification_status='official'`` — the MCP
  Registry IS the canonical directory for MCP servers.
* App stays ``status='draft'`` — ingest never publishes on its own.
* Idempotent across repeated runs (no duplicate rows).
* Schema-mismatched rows land in ``UnparsedRegistryRecord``, not the worker
  crash log.
* Empty iteration (e.g. upstream registry unreachable) reports zeros without
  raising — Celery must not enter a retry loop on transient upstream outages.
* Per-record upsert failures are isolated; one bad draft never aborts the batch.
"""
from __future__ import annotations

import pytest

from apps.catalog.models import App, AppPlatform, Platform
from apps.sources import tasks
from apps.sources.base import AppDraft
from apps.sources.models import Source, UnparsedRegistryRecord


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def mcp_platform() -> Platform:
    """Ensure the canonical MCP platform row exists.

    ``apps.sources.upsert.attach_platforms`` silently skips unknown platform
    slugs, so without this row the test DB would never get an ``AppPlatform``
    row and the assertions would be vacuous.
    """
    platform, _ = Platform.objects.get_or_create(
        slug="mcp",
        defaults={
            "name": "MCP",
            "public_path": "mcp-servers",
            "website_url": "https://modelcontextprotocol.io/",
        },
    )
    return platform


def _make_draft(
    external_id: str = "server-001",
    name: str = "ExampleMCP",
) -> AppDraft:
    return AppDraft(
        name=name,
        slug_hint=name.lower(),
        short_description="An example MCP server",
        long_description="Longer description",
        developer_name="Acme",
        developer_url="https://acme.example",
        official_page_url="https://example.com/mcp",
        repo_url="https://github.com/acme/example",
        platforms=["mcp"],
        listing_types=["mcp-server"],
        capabilities={"remote_available": "yes", "open_source": "yes"},
        external_id=external_id,
        raw_payload={"id": external_id, "name": name},
        official_directory_url=(
            f"https://registry.modelcontextprotocol.io/v1/servers/{external_id}"
        ),
        platform_metadata={"protocol_version": "2025-03-26", "transport": "stdio"},
    )


class _FakeSource:
    """Stand-in for ``MCPRegistrySource`` that yields predetermined drafts.

    Isolating the HTTP layer keeps these tests deterministic and fast; the
    real ``MCPRegistrySource`` is exercised by its own unit tests (out of
    scope for Phase 0).
    """

    source_type = Source.SourceType.MCP_REGISTRY

    def __init__(
        self,
        drafts: list[AppDraft] | None = None,
        unparsed: list[dict] | None = None,
        versions: set[str] | None = None,
    ) -> None:
        self._drafts = drafts or []
        self.unparsed = unparsed or []
        self.observed_schema_versions = versions or set()

    def iter_drafts(self):
        return iter(self._drafts)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_creates_app_with_appplatform_and_official_status(
    monkeypatch, mcp_platform
) -> None:
    draft = _make_draft(external_id="creates-001", name="CreatesNew")
    monkeypatch.setattr(
        tasks,
        "MCPRegistrySource",
        lambda: _FakeSource(drafts=[draft], versions={"1.0"}),
    )

    result = tasks.ingest_mcp_registry()

    assert result == {"new": 1, "updated": 0, "skipped": 0, "failed": 0}
    app = App.objects.get(slug="createsnew")
    assert app.status == App.AppStatus.DRAFT
    assert (
        app.platform_verification_status
        == App.PlatformVerificationStatus.OFFICIAL
    )
    assert (
        app.editorial_review_status
        == App.EditorialReviewStatus.UNREVIEWED
    )
    assert AppPlatform.objects.filter(app=app, platform=mcp_platform).exists()
    assert Source.objects.filter(
        app=app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="creates-001",
    ).exists()


def test_is_idempotent_on_repeated_runs(monkeypatch, mcp_platform) -> None:
    draft = _make_draft(external_id="idem-002", name="IdemTest")
    monkeypatch.setattr(
        tasks,
        "MCPRegistrySource",
        lambda: _FakeSource(drafts=[draft], versions={"1.0"}),
    )

    first = tasks.ingest_mcp_registry()
    app_count = App.objects.count()
    source_count = Source.objects.count()
    appplatform_count = AppPlatform.objects.count()

    second = tasks.ingest_mcp_registry()

    assert first == {"new": 1, "updated": 0, "skipped": 0, "failed": 0}
    assert second == {"new": 0, "updated": 1, "skipped": 0, "failed": 0}
    assert App.objects.count() == app_count
    assert Source.objects.count() == source_count
    assert AppPlatform.objects.count() == appplatform_count


def test_routes_schema_mismatches_to_unparsed_registry_record(
    monkeypatch, mcp_platform
) -> None:
    bad_record = {"description": "missing name and id"}
    fake_unparsed = [
        {
            "record": bad_record,
            "error": "missing required field 'id'",
            "schema_version": "1.0",
        }
    ]
    monkeypatch.setattr(
        tasks,
        "MCPRegistrySource",
        lambda: _FakeSource(drafts=[], unparsed=fake_unparsed, versions={"1.0"}),
    )

    assert UnparsedRegistryRecord.objects.count() == 0
    result = tasks.ingest_mcp_registry()

    assert result == {"new": 0, "updated": 0, "skipped": 0, "failed": 0}
    assert UnparsedRegistryRecord.objects.count() == 1
    record = UnparsedRegistryRecord.objects.first()
    assert record.payload == bad_record
    assert "missing required field" in record.error
    assert record.schema_version == "1.0"


def test_empty_iteration_does_not_raise(monkeypatch, mcp_platform) -> None:
    """Upstream registry unreachable → MCPRegistrySource yields nothing.

    Task must report zeros and not raise; otherwise Celery enters a retry
    loop on a transient outage that the next beat tick would have handled.
    """
    monkeypatch.setattr(
        tasks, "MCPRegistrySource", lambda: _FakeSource(drafts=[])
    )

    result = tasks.ingest_mcp_registry()

    assert result == {"new": 0, "updated": 0, "skipped": 0, "failed": 0}


def test_per_record_upsert_failure_is_isolated(monkeypatch, mcp_platform) -> None:
    good = _make_draft(external_id="good-001", name="GoodOne")
    bad = _make_draft(external_id="bad-001", name="BadOne")

    real_upsert = tasks.upsert_app_from_draft
    call_count = {"n": 0}

    def flaky_upsert(draft, source_type):
        call_count["n"] += 1
        if draft.external_id == "bad-001":
            raise RuntimeError("synthetic failure")
        return real_upsert(draft, source_type)

    monkeypatch.setattr(
        tasks, "MCPRegistrySource", lambda: _FakeSource(drafts=[good, bad])
    )
    monkeypatch.setattr(tasks, "upsert_app_from_draft", flaky_upsert)

    result = tasks.ingest_mcp_registry()

    assert call_count["n"] == 2
    assert result == {"new": 1, "updated": 0, "skipped": 0, "failed": 1}
    assert App.objects.filter(slug="goodone").exists()
    assert not App.objects.filter(slug="badone").exists()
