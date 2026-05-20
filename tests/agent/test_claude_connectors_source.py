from __future__ import annotations

from apps.agent.sources import claude_connectors as cc
from apps.agent.sources.claude_connectors import (
    ClaudeConnectorsSource,
    card_to_draft,
    parse_detail_page,
    parse_index_page,
)


ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
ROBOTS_DISALLOW = "User-agent: *\nDisallow: /connectors\n"

INDEX_HTML = """
<main>
  <article data-connector-card data-name="Acme Docs"
      data-description="Search and summarize Acme documents."
      data-categories="Development Tools,Healthcare"
      data-products="Claude,Claude Code"
      data-publication-date="2026-05-19">
    <a href="/connectors/acme-docs">Acme Docs</a>
    <img src="https://cdn.example/acme.png">
  </article>
</main>
"""

DETAIL_HTML = """
<main data-developer-name="Acme Inc" data-long-description="Long Acme connector description.">
  <h1>Acme Docs</h1>
  <a data-developer-url href="https://acme.example">Developer website</a>
  <a data-official-url href="https://acme.example/connect">Connect</a>
  <p>Requires OAuth authorization. Can read, create, and update documents.</p>
</main>
"""


def test_parses_claude_connector_into_draft() -> None:
    card = parse_index_page(INDEX_HTML, base_url="https://claude.com/connectors")[0]
    detail = parse_detail_page(
        DETAIL_HTML,
        detail_url="https://claude.com/connectors/acme-docs",
    )

    draft = card_to_draft(
        card,
        detail,
        detail_url="https://claude.com/connectors/acme-docs",
    )

    assert draft.name == "Acme Docs"
    assert draft.developer_name == "Acme Inc"
    assert draft.official_page_url == "https://acme.example/connect"
    assert draft.platforms == ["claude"]
    assert draft.listing_types == ["claude-connector"]
    assert draft.categories == ["developer-tools"]
    assert draft.raw_payload["unmapped_categories"] == ["Healthcare"]
    assert draft.capabilities["read_data"] == "yes"


def test_source_respects_robots_disallow() -> None:
    cc._ROBOTS_CACHE.clear()
    fetched = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return ROBOTS_DISALLOW

    source = ClaudeConnectorsSource(
        base_url="https://claude.com/connectors",
        fetch_text=fetch,
    )

    assert list(source.iter_drafts()) == []
    assert fetched == ["https://claude.com/robots.txt"]


def test_source_full_crawl_yields_draft() -> None:
    cc._ROBOTS_CACHE.clear()
    responses = {
        "https://claude.com/robots.txt": ROBOTS_ALLOW,
        "https://claude.com/connectors": INDEX_HTML,
        "https://claude.com/connectors/acme-docs": DETAIL_HTML,
    }
    source = ClaudeConnectorsSource(
        base_url="https://claude.com/connectors",
        fetch_text=lambda url: responses[url],
    )

    drafts = list(source.iter_drafts())

    assert len(drafts) == 1
    assert drafts[0].external_id == "claude:acme-docs"
