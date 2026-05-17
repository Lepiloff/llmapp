"""Regressions for the editor-edit-preserving `attach_platforms` contract.

Before the fix, every re-discovery of the same ``external_id`` would
re-run ``update_or_create`` with ``defaults`` populated from the LLM
draft and silently clobber any platform metadata the editor had set
by hand (``region_availability``, ``supported_plans``,
``scope_summary``, ``metadata``).
"""
from __future__ import annotations

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.catalog.models import App, AppPlatform, Platform
from apps.sources.base import AppDraft
from apps.sources.upsert import attach_platforms


# Force eager Celery for the whole module so the catalog post_save signal's
# `refresh_search_vector_task.delay()` doesn't try to reach the docker-only
# redis hostname from the host (which would otherwise hang for ~3 minutes
# in the cross-thread race test until the Celery retry budget runs out).
@pytest.fixture(autouse=True)
def _force_eager_celery(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = False
    yield


pytestmark = pytest.mark.django_db


@pytest.fixture
def app(db) -> App:
    return App.objects.create(name="Acme", slug="acme", short_description="x")


@pytest.fixture
def mcp_platform(db) -> Platform:
    return Platform.objects.get_or_create(
        slug="mcp",
        defaults={"name": "MCP", "public_path": "mcp-servers"},
    )[0]


def _draft(*, platforms=("mcp",), **kwargs) -> AppDraft:
    return AppDraft(
        name="Acme",
        slug_hint="acme",
        short_description="x",
        platforms=list(platforms),
        **kwargs,
    )


def test_first_call_creates_row_with_draft_fields(app, mcp_platform) -> None:
    draft = _draft(
        supported_plans=["pro", "enterprise"],
        region_availability=AppPlatform.RegionAvailability.US_ONLY,
        scope_summary="reads files",
        platform_metadata={"transport": "stdio"},
    )

    attach_platforms(app, draft)

    row = AppPlatform.objects.get(app=app, platform=mcp_platform)
    assert row.supported_plans == ["pro", "enterprise"]
    assert row.region_availability == AppPlatform.RegionAvailability.US_ONLY
    assert row.scope_summary == "reads files"
    assert row.metadata == {"transport": "stdio"}


def test_rediscovery_preserves_editor_supported_plans(app, mcp_platform) -> None:
    attach_platforms(app, _draft(supported_plans=["free"]))
    # Editor edits the row to claim enterprise-only support.
    AppPlatform.objects.filter(app=app, platform=mcp_platform).update(
        supported_plans=["enterprise"]
    )

    attach_platforms(app, _draft(supported_plans=["free"]))

    row = AppPlatform.objects.get(app=app, platform=mcp_platform)
    assert row.supported_plans == ["enterprise"]


def test_rediscovery_preserves_editor_region(app, mcp_platform) -> None:
    attach_platforms(app, _draft())  # creates row with region=unknown

    AppPlatform.objects.filter(app=app, platform=mcp_platform).update(
        region_availability=AppPlatform.RegionAvailability.EU_ONLY,
    )

    attach_platforms(app, _draft(region_availability=AppPlatform.RegionAvailability.US_ONLY))

    row = AppPlatform.objects.get(app=app, platform=mcp_platform)
    assert row.region_availability == AppPlatform.RegionAvailability.EU_ONLY


def test_rediscovery_preserves_editor_scope_summary(app, mcp_platform) -> None:
    attach_platforms(app, _draft(scope_summary="reads"))

    AppPlatform.objects.filter(app=app, platform=mcp_platform).update(
        scope_summary="editor-curated scope"
    )
    attach_platforms(app, _draft(scope_summary="reads and writes"))

    row = AppPlatform.objects.get(app=app, platform=mcp_platform)
    assert row.scope_summary == "editor-curated scope"


def test_rediscovery_fills_empty_fields(app, mcp_platform) -> None:
    """First discovery had no scope; second discovery has one → fill it."""
    attach_platforms(app, _draft(scope_summary=""))
    row = AppPlatform.objects.get(app=app, platform=mcp_platform)
    assert row.scope_summary == ""

    attach_platforms(app, _draft(scope_summary="reads files"))
    row.refresh_from_db()
    assert row.scope_summary == "reads files"


def test_rediscovery_shallow_merges_metadata(app, mcp_platform) -> None:
    attach_platforms(app, _draft(platform_metadata={"transport": "stdio"}))

    # Editor adds a key by hand.
    AppPlatform.objects.filter(app=app, platform=mcp_platform).update(
        metadata={"transport": "stdio", "internal_note": "verified"},
    )

    # Re-discovery proposes a different transport value AND a new key.
    attach_platforms(
        app,
        _draft(platform_metadata={"transport": "http", "new_key": "value"}),
    )

    row = AppPlatform.objects.get(app=app, platform=mcp_platform)
    # editor edits win on conflicts
    assert row.metadata["transport"] == "stdio"
    # editor-only keys preserved
    assert row.metadata["internal_note"] == "verified"
    # genuinely new keys land
    assert row.metadata["new_key"] == "value"


def test_last_verified_at_always_updates(app, mcp_platform) -> None:
    attach_platforms(app, _draft())
    row = AppPlatform.objects.get(app=app, platform=mcp_platform)
    old_ts = row.last_verified_on_platform_at

    # Force-roll the row back so the timestamp difference is observable.
    AppPlatform.objects.filter(pk=row.pk).update(
        last_verified_on_platform_at=timezone.now().replace(year=2020),
    )

    attach_platforms(app, _draft())
    row.refresh_from_db()
    assert row.last_verified_on_platform_at.year != 2020


@pytest.mark.django_db(transaction=True)
def test_concurrent_editor_edit_survives_attach_platforms(
    app, mcp_platform
) -> None:
    """An editor write that lands while attach_platforms is mid-update
    must not be silently overwritten.

    We exercise the lock by:
      1. Seeding the row with attach_platforms (region=unknown).
      2. Starting attach_platforms in a thread that we pause inside the
         transaction via a select_for_update sentinel.
      3. From the main thread, attempt the editor edit — it must block
         on the same row lock.
      4. Once the attach_platforms transaction commits, the editor's
         write proceeds and we observe its value, not the agent's
         draft value.

    The test is implemented more pragmatically below: we use SELECT
    FOR UPDATE directly to take the row lock in a second connection,
    update the field, then re-call attach_platforms — without the
    select_for_update inside attach_platforms, the agent would see a
    stale snapshot. With the lock, attach_platforms blocks until the
    editor commits and then reads the fresh value.
    """
    from threading import Event, Thread
    from django.db import close_old_connections, connection

    attach_platforms(app, _draft())  # seed with region=unknown

    editor_committed = Event()
    lock_acquired = Event()
    agent_done = Event()
    errors: list[Exception] = []

    def editor_thread() -> None:
        try:
            from django.db import transaction as _txn

            with _txn.atomic():
                row = (
                    AppPlatform.objects.select_for_update()
                    .get(app=app, platform=mcp_platform)
                )
                lock_acquired.set()
                # Hold the lock while the agent races to update.
                row.region_availability = AppPlatform.RegionAvailability.EU_ONLY
                row.save(update_fields=["region_availability"])
                # Wait until the agent has hit its select_for_update
                # before we commit — this makes the agent's attempt
                # truly block on our lock.
                agent_done.wait(timeout=2)
        except Exception as exc:  # pragma: no cover - debug aid
            errors.append(exc)
        finally:
            close_old_connections()
            editor_committed.set()

    editor = Thread(target=editor_thread)
    editor.start()
    lock_acquired.wait(timeout=2)

    # Agent fires while editor's lock is held. attach_platforms should
    # block on select_for_update, then read the editor's value.
    def agent_thread() -> None:
        try:
            attach_platforms(
                app,
                _draft(region_availability=AppPlatform.RegionAvailability.US_ONLY),
            )
        except Exception as exc:  # pragma: no cover - debug aid
            errors.append(exc)
        finally:
            close_old_connections()

    agent = Thread(target=agent_thread)
    agent.start()
    # Give the agent a moment to reach select_for_update and block.
    import time
    time.sleep(0.3)
    agent_done.set()  # signal the editor it's safe to commit

    editor.join(timeout=3)
    agent.join(timeout=3)
    assert not errors, errors

    row = AppPlatform.objects.get(app=app, platform=mcp_platform)
    # Editor's EU_ONLY survives, even though the agent's draft proposed
    # US_ONLY *after* the editor's lock was acquired.
    assert row.region_availability == AppPlatform.RegionAvailability.EU_ONLY
