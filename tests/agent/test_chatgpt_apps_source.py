from __future__ import annotations

from apps.agent.sources import chatgpt_apps as ca
from apps.agent.sources.chatgpt_apps import (
    ChatGPTAppsSource,
    card_to_draft,
    parse_detail_page,
    parse_index_page,
)


ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"

INDEX_HTML = """
<main>
  <a href="/app/acme-chat" class="app-row">
    <div class="app-icon"><img src="https://cdn.example/acme.webp"></div>
    <div class="app-meta">
      <p class="app-name">Acme Chat
        <span class="surface-badges">
          <span class="surface-badge chatgpt" title="ChatGPT App"></span>
          <span class="surface-badge claude" title="Claude Connector"></span>
        </span>
      </p>
      <p class="app-tag">Search Acme docs from ChatGPT</p>
    </div>
  </a>
  <nav aria-label="Pagination"><a href="/chatgpt-apps?page=2">Next</a></nav>
</main>
"""

DETAIL_HTML = """
<main>
  <header class="app-head">
    <div class="app-head-text">
      <h1>Acme Chat</h1>
      <p>Search Acme docs from ChatGPT</p>
    </div>
    <a class="btn-connect" href="https://chatgpt.com/apps/acme-chat/connector_123">Connect</a>
  </header>
  <section class="detail-section prose-grid">
    <p>Acme Chat lets ChatGPT search, summarize, and update Acme documents.</p>
  </section>
  <section class="detail-section">
    <h2>Information</h2>
    <div class="info-table">
      <div class="info-row"><div class="info-key">Category</div><div class="info-val"><a href="/category/productivity">Productivity</a><a href="/category/files">Files</a></div></div>
      <div class="info-row"><div class="info-key">Capabilities</div><div class="info-val">Reads, Writes, Interactive</div></div>
      <div class="info-row"><div class="info-key">Developer</div><div class="info-val">Acme Inc</div></div>
      <div class="info-row"><div class="info-key">Website</div><div class="info-val"><a href="https://acme.example">https://acme.example</a></div></div>
      <div class="info-row"><div class="info-key">Auth</div><div class="info-val">oauth</div></div>
      <div class="info-row"><div class="info-key">Transport</div><div class="info-val">http</div></div>
      <div class="info-row"><div class="info-key">Version</div><div class="info-val">1.2.3</div></div>
    </div>
  </section>
</main>
"""


def test_parses_chatgpt_app_into_draft() -> None:
    card = parse_index_page(INDEX_HTML, base_url="https://mcpapp.net/chatgpt-apps")[0]
    detail = parse_detail_page(
        DETAIL_HTML,
        detail_url="https://mcpapp.net/app/acme-chat",
    )

    draft = card_to_draft(
        card,
        detail,
        detail_url="https://mcpapp.net/app/acme-chat",
    )

    assert draft.name == "Acme Chat"
    assert draft.platforms == ["chatgpt", "claude"]
    assert draft.listing_types == ["chatgpt-app", "claude-connector"]
    assert draft.categories == ["files", "productivity"]
    assert draft.official_page_url == "https://chatgpt.com/apps/acme-chat/connector_123"
    assert draft.developer_name == "Acme Inc"
    assert draft.capabilities["read_data"] == "yes"
    assert draft.capabilities["write_actions"] == "yes"
    assert draft.capabilities["interactive_ui"] == "yes"
    assert draft.capabilities["auth_required"] == "yes"
    assert draft.capabilities["remote_available"] == "yes"
    assert draft.external_id == "mcpapp-chatgpt:acme-chat"


def test_source_full_crawl_yields_draft() -> None:
    ca._ROBOTS_CACHE.clear()
    responses = {
        "https://mcpapp.net/robots.txt": ROBOTS_ALLOW,
        "https://mcpapp.net/chatgpt-apps": INDEX_HTML,
        "https://mcpapp.net/app/acme-chat": DETAIL_HTML,
    }
    source = ChatGPTAppsSource(
        index_url="https://mcpapp.net/chatgpt-apps",
        fetch_text=lambda url: responses[url],
        max_pages=1,
    )

    drafts = list(source.iter_drafts())

    assert len(drafts) == 1
    assert drafts[0].raw_payload["source_kind"] == "third_party_chatgpt_apps_index"


def test_source_without_max_pages_follows_pagination_until_last_page() -> None:
    ca._ROBOTS_CACHE.clear()
    page_2 = INDEX_HTML.replace(
        "/app/acme-chat",
        "/app/beta-chat",
    ).replace(
        "Acme Chat",
        "Beta Chat",
    ).replace(
        '<nav aria-label="Pagination"><a href="/chatgpt-apps?page=2">Next</a></nav>',
        "",
    )
    detail_2 = DETAIL_HTML.replace("Acme Chat", "Beta Chat")
    responses = {
        "https://mcpapp.net/robots.txt": ROBOTS_ALLOW,
        "https://mcpapp.net/chatgpt-apps": INDEX_HTML,
        "https://mcpapp.net/chatgpt-apps?page=2": page_2,
        "https://mcpapp.net/app/acme-chat": DETAIL_HTML,
        "https://mcpapp.net/app/beta-chat": detail_2,
    }
    fetched = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return responses[url]

    source = ChatGPTAppsSource(
        index_url="https://mcpapp.net/chatgpt-apps",
        fetch_text=fetch,
    )

    drafts = list(source.iter_drafts())

    assert [draft.name for draft in drafts] == ["Acme Chat", "Beta Chat"]
    assert "https://mcpapp.net/chatgpt-apps?page=2" in fetched


def test_source_stops_when_next_page_repeats_same_cards() -> None:
    ca._ROBOTS_CACHE.clear()
    repeated_page = INDEX_HTML.replace(
        "/chatgpt-apps?page=2",
        "/chatgpt-apps?page=3",
    )
    responses = {
        "https://mcpapp.net/robots.txt": ROBOTS_ALLOW,
        "https://mcpapp.net/chatgpt-apps": INDEX_HTML,
        "https://mcpapp.net/chatgpt-apps?page=2": repeated_page,
        "https://mcpapp.net/app/acme-chat": DETAIL_HTML,
    }
    fetched = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return responses[url]

    source = ChatGPTAppsSource(
        index_url="https://mcpapp.net/chatgpt-apps",
        fetch_text=fetch,
    )

    drafts = list(source.iter_drafts())

    assert [draft.name for draft in drafts] == ["Acme Chat"]
    assert "https://mcpapp.net/chatgpt-apps?page=2" in fetched
    assert "https://mcpapp.net/chatgpt-apps?page=3" not in fetched
