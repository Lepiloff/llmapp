from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from apps.catalog.models import App, AppPlatform, Platform
from apps.sources.models import Source


pytestmark = pytest.mark.django_db


def _mcp_payload(*, name: str, title: str) -> dict:
    return {
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "isLatest": True,
                "status": "active",
            }
        },
        "server": {
            "name": name,
            "title": title,
            "version": "1.0.0",
            "description": f"{title} description",
            "repository": {"url": "https://github.com/acme/mcp-monorepo"},
            "remotes": [
                {
                    "type": "streamable-http",
                    "url": f"https://example.com/{name.rsplit('/', 1)[-1]}/mcp",
                }
            ],
        },
    }


@pytest.fixture
def mcp_platform() -> Platform:
    platform, _ = Platform.objects.get_or_create(
        slug="mcp",
        defaults={
            "name": "MCP",
            "public_path": "mcp-servers",
            "website_url": "https://modelcontextprotocol.io/",
        },
    )
    return platform


def _bad_merge_state() -> tuple[App, Source]:
    app = App.objects.create(
        name="Acme Alpha MCP",
        slug="acme-alpha-mcp",
        short_description="Alpha description",
        official_page_url="https://github.com/acme/mcp-monorepo",
        repo_url="https://github.com/acme/mcp-monorepo",
        status=App.AppStatus.DRAFT,
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="com.acme/alpha",
        source_url="https://registry.modelcontextprotocol.io/v0/servers/com.acme%2Falpha",
        payload=_mcp_payload(name="com.acme/alpha", title="Acme Alpha MCP"),
        is_primary=True,
    )
    beta = Source.objects.create(
        app=app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="com.acme/beta",
        source_url="https://registry.modelcontextprotocol.io/v0/servers/com.acme%2Fbeta",
        payload=_mcp_payload(name="com.acme/beta", title="Acme Beta MCP"),
        is_primary=False,
    )
    return app, beta


def test_dry_run_reports_candidates_without_writing(mcp_platform) -> None:
    app, beta = _bad_merge_state()
    out = StringIO()

    call_command("repair_mcp_registry_splits", "--dry-run", stdout=out)

    assert "candidates=1" in out.getvalue()
    assert App.objects.count() == 1
    beta.refresh_from_db()
    assert beta.app == app
    assert beta.is_primary is False


def test_repair_splits_non_primary_mcp_source_into_own_app(mcp_platform) -> None:
    app, beta = _bad_merge_state()
    out = StringIO()

    call_command("repair_mcp_registry_splits", stdout=out)

    assert "split=1" in out.getvalue()
    assert App.objects.count() == 2
    assert Source.objects.filter(app=app, source_type=Source.SourceType.MCP_REGISTRY).count() == 1
    beta.refresh_from_db()
    assert beta.app != app
    assert beta.is_primary is True
    assert beta.app.name == "Acme Beta MCP"
    assert beta.app.short_description == "Acme Beta MCP description"
    assert beta.app.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL
    assert AppPlatform.objects.filter(app=beta.app, platform=mcp_platform).exists()
