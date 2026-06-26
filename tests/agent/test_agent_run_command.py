"""End-to-end smoke test for ``manage.py agent_run``.

Confirms the operator-facing surface:

* ``--enrich-app=<slug> --dry-run`` runs without touching App.
* Without ``--dry-run`` (i.e. ``--apply``), App / AppCapability writes
  land.
* Either mode writes ``AgentRun`` / ``EnrichmentTask`` / ``LLMCallLog``
  rows for audit.

The test substitutes ``MockLLMProvider`` for the configured primary
provider via ``settings.AGENT_LLM_PROVIDER_PRIMARY='mock'`` (default).
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.agent.llm import client as agent_client
from apps.agent.llm.schemas import CapabilityProposal, MergeSet
from apps.agent.models import AgentRun, EnrichmentTask, LLMCallLog, NeedsReviewQueueEntry
from apps.catalog.models import App, AppCapability, Capability, Platform

pytestmark = pytest.mark.django_db


@pytest.fixture
def draft_app() -> App:
    from apps.sources.models import Source

    Platform.objects.get_or_create(
        slug="mcp", defaults={"name": "MCP", "public_path": "mcp-servers"}
    )
    Capability.objects.get_or_create(key="open_source", defaults={"label": "Open source"})
    app = App.objects.create(
        name="SmokeTest",
        slug="smoke-test",
        short_description="",
        status=App.AppStatus.DRAFT,
    )
    app.platforms.add(Platform.objects.get(slug="mcp"))
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id=f"mcp-registry:{app.slug}",
        is_primary=True,
    )
    return app


@pytest.fixture
def patched_provider(monkeypatch) -> None:
    """Force build_provider to return a deterministic mock for the test."""
    def fake_build(role: str, **kwargs):
        return agent_client.MockLLMProvider(
            responses_queue=[
                MergeSet(
                    short_description="Smoke-test description",
                    capabilities={
                        "open_source": CapabilityProposal(
                            value="yes", evidence="GH README"
                        ),
                    },
                    proposed_verdict="A useful little tool",
                )
            ]
        )
    monkeypatch.setattr(agent_client, "build_provider", fake_build)
    # Also patch the import-site used inside tasks (avoids stale binding).
    from apps.agent import tasks as agent_tasks
    monkeypatch.setattr(agent_tasks, "build_provider", fake_build)


def test_dry_run_does_not_modify_app(draft_app, patched_provider) -> None:
    out = StringIO()
    call_command("agent_run", f"--enrich-app={draft_app.slug}", stdout=out)

    draft_app.refresh_from_db()
    # Dry-run: no field changes in App.
    assert draft_app.short_description == ""
    assert draft_app.status == App.AppStatus.DRAFT
    assert not AppCapability.objects.filter(app=draft_app).exists()
    # Audit rows DO get written even in dry-run.
    assert AgentRun.objects.filter(status=AgentRun.Status.DRY_RUN).exists()
    task = EnrichmentTask.objects.get(app=draft_app)
    assert task.status == EnrichmentTask.Status.DRY_RUN
    assert LLMCallLog.objects.filter(task=task, is_mock=True).count() == 1
    # The proposed verdict surfaces in stdout for the operator.
    assert "useful little tool" in out.getvalue()


def test_apply_writes_to_db(draft_app, patched_provider) -> None:
    out = StringIO()
    call_command("agent_run", f"--enrich-app={draft_app.slug}", "--apply", stdout=out)

    draft_app.refresh_from_db()
    assert draft_app.short_description == "Smoke-test description"
    assert draft_app.status == App.AppStatus.DRAFT  # invariant
    assert draft_app.verdict == ""  # invariant
    cap = AppCapability.objects.get(app=draft_app, capability__key="open_source")
    assert cap.value == AppCapability.CapabilityValue.YES
    # proposed_verdict landed in review queue, not in App.verdict.
    entry = NeedsReviewQueueEntry.objects.get(app=draft_app)
    assert entry.payload["proposed_verdict"] == "A useful little tool"


def test_unknown_slug_raises(patched_provider) -> None:
    from django.core.management.base import CommandError
    with pytest.raises(CommandError):
        call_command("agent_run", "--enrich-app=does-not-exist")


@override_settings(AGENT_ENRICH_PENDING_SOURCE_TYPES=["gemini_extensions"])
def test_enrich_pending_command_uses_source_type_allowlist(monkeypatch) -> None:
    from apps.agent.management.commands.agent_run import Command
    from apps.sources.models import Source

    mcp_app = App.objects.create(
        name="MCP Pending",
        slug="mcp-pending-command",
        status=App.AppStatus.DRAFT,
    )
    Source.objects.create(
        app=mcp_app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="mcp-registry:mcp-pending-command",
    )
    gemini_app = App.objects.create(
        name="Gemini Pending",
        slug="gemini-pending-command",
        status=App.AppStatus.DRAFT,
    )
    Source.objects.create(
        app=gemini_app,
        source_type=Source.SourceType.GEMINI_EXTENSIONS,
        external_id="gemini:gemini-pending-command",
    )

    processed: list[int] = []

    def fake_process_one(self, app_id, *, dry_run, allow_non_mcp):
        processed.append(app_id)

    monkeypatch.setattr(Command, "_process_one", fake_process_one)

    call_command("agent_run", "--enrich-pending", "--limit=10", stdout=StringIO())

    assert processed == [gemini_app.pk]


def test_published_app_is_rejected_with_command_error(
    draft_app, patched_provider
) -> None:
    """Phase 1 invariant: only DRAFT apps eligible. PUBLISHED → CommandError."""
    from django.core.management.base import CommandError
    App.objects.filter(pk=draft_app.pk).update(status=App.AppStatus.PUBLISHED)

    with pytest.raises(CommandError, match="not eligible"):
        call_command("agent_run", f"--enrich-app={draft_app.slug}", "--apply")

    draft_app.refresh_from_db()
    assert draft_app.status == App.AppStatus.PUBLISHED
    assert draft_app.short_description == ""
    # No audit Source row written (transaction never started).
    assert not draft_app.sources.filter(
        external_id=f"agent-enrich:{draft_app.pk}"
    ).exists()


def test_source_rss_invokes_discovery_task(monkeypatch) -> None:
    from apps.agent.management.commands import agent_run

    called = {}

    def fake_discover_rss(*, limit: int, dry_run: bool):
        called["limit"] = limit
        called["dry_run"] = dry_run
        return {"seen": 2, "relevant": 1}

    monkeypatch.setattr(agent_run, "discover_rss", fake_discover_rss)
    out = StringIO()

    call_command("agent_run", "--source=rss", "--limit=2", stdout=out)

    assert called == {"limit": 2, "dry_run": True}
    assert "[DRY-RUN] source=rss" in out.getvalue()
    assert '"relevant": 1' in out.getvalue()


def test_source_github_apply_invokes_discovery_task(monkeypatch) -> None:
    from apps.agent.management.commands import agent_run

    called = {}

    def fake_discover_github_mcp(*, limit: int, dry_run: bool):
        called["limit"] = limit
        called["dry_run"] = dry_run
        return {"seen": 1, "persisted": 1}

    monkeypatch.setattr(agent_run, "discover_github_mcp", fake_discover_github_mcp)
    out = StringIO()

    call_command(
        "agent_run",
        "--source=github_mcp",
        "--limit=1",
        "--apply",
        stdout=out,
    )

    assert called == {"limit": 1, "dry_run": False}
    assert "[APPLIED] source=github_mcp" in out.getvalue()
    assert '"persisted": 1' in out.getvalue()


def test_source_gemini_apply_invokes_direct_ingest_with_flag_bypass(monkeypatch) -> None:
    from apps.agent.management.commands import agent_run

    called = {}

    def fake_ingest_gemini_extensions(
        *,
        limit: int,
        dry_run: bool,
        enforce_flag: bool,
        trigger: str,
    ):
        called["limit"] = limit
        called["dry_run"] = dry_run
        called["enforce_flag"] = enforce_flag
        called["trigger"] = trigger
        return {"seen": 1, "new": 1}

    monkeypatch.setattr(agent_run, "ingest_gemini_extensions", fake_ingest_gemini_extensions)
    out = StringIO()

    call_command(
        "agent_run",
        "--source=gemini_extensions",
        "--limit=1",
        "--apply",
        stdout=out,
    )

    assert called == {
        "limit": 1,
        "dry_run": False,
        "enforce_flag": False,
        "trigger": "manual",
    }
    assert "[APPLIED] source=gemini_extensions" in out.getvalue()
    assert '"new": 1' in out.getvalue()


def test_source_chatgpt_apply_invokes_direct_ingest_with_flag_bypass(monkeypatch) -> None:
    from apps.agent.management.commands import agent_run

    called = {}

    def fake_ingest_chatgpt_apps(
        *,
        limit: int,
        dry_run: bool,
        enforce_flag: bool,
        trigger: str,
    ):
        called["limit"] = limit
        called["dry_run"] = dry_run
        called["enforce_flag"] = enforce_flag
        called["trigger"] = trigger
        return {"seen": 1, "new": 1}

    monkeypatch.setattr(agent_run, "ingest_chatgpt_apps", fake_ingest_chatgpt_apps)
    out = StringIO()

    call_command(
        "agent_run",
        "--source=chatgpt_apps",
        "--limit=1",
        "--apply",
        stdout=out,
    )

    assert called == {
        "limit": 1,
        "dry_run": False,
        "enforce_flag": False,
        "trigger": "manual",
    }
    assert "[APPLIED] source=chatgpt_apps" in out.getvalue()
    assert '"new": 1' in out.getvalue()


def test_source_mcp_registry_apply_accepts_resume_options(monkeypatch) -> None:
    from apps.agent.management.commands import agent_run

    called = {}

    def fake_ingest_mcp_registry(*, start_cursor=None, request_timeout=None):
        called["start_cursor"] = start_cursor
        called["request_timeout"] = request_timeout
        return {"new": 0, "updated": 1, "skipped": 0, "failed": 0}

    monkeypatch.setattr(agent_run, "ingest_mcp_registry", fake_ingest_mcp_registry)
    out = StringIO()

    call_command(
        "agent_run",
        "--source=mcp_registry",
        "--apply",
        "--mcp-start-cursor=resume-from-here",
        "--mcp-timeout=123",
        stdout=out,
    )

    assert called == {
        "start_cursor": "resume-from-here",
        "request_timeout": 123.0,
    }
    assert "[APPLIED] source=mcp_registry" in out.getvalue()
    assert '"updated": 1' in out.getvalue()
