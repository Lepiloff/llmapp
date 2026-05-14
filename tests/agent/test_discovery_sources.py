from __future__ import annotations

import base64

from apps.agent.sources.github_mcp_search import (
    candidate_to_minimal_draft,
    fetch_github_readme_text,
    parse_search_response,
)
from apps.agent.sources.rss_feeds import parse_feed


def test_parse_rss_feed_items() -> None:
    xml = """
    <rss><channel>
      <item>
        <title>New MCP server for Acme</title>
        <link>https://example.com/acme-mcp</link>
        <description><![CDATA[<p>Connect Claude to Acme.</p>]]></description>
        <pubDate>Wed, 13 May 2026 10:00:00 GMT</pubDate>
      </item>
    </channel></rss>
    """

    candidates = parse_feed(xml, feed_url="https://example.com/feed.xml")

    assert len(candidates) == 1
    assert candidates[0].external_id == "rss:https://example.com/acme-mcp"
    assert candidates[0].title == "New MCP server for Acme"
    assert candidates[0].summary == "Connect Claude to Acme."


def test_parse_atom_feed_entries() -> None:
    xml = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>repo/example-mcp</title>
        <link href="https://github.com/repo/example-mcp"/>
        <summary>MCP server for examples.</summary>
        <updated>2026-05-13T10:00:00Z</updated>
      </entry>
    </feed>
    """

    candidates = parse_feed(xml, feed_url="https://github.com/topics/mcp-server.atom")

    assert len(candidates) == 1
    assert candidates[0].url == "https://github.com/repo/example-mcp"
    assert candidates[0].source_name == "rss"


def test_parse_github_search_response_skips_malformed_items() -> None:
    payload = {
        "items": [
            {
                "id": 10,
                "full_name": "acme/acme-mcp",
                "html_url": "https://github.com/acme/acme-mcp",
                "description": "Acme MCP server",
                "stargazers_count": 42,
                "pushed_at": "2026-05-13T10:00:00Z",
                "owner": {"login": "acme"},
            },
            {"full_name": "missing-url"},
            "bad",
        ]
    }

    candidates = parse_search_response(payload)

    assert len(candidates) == 1
    assert candidates[0].external_id == "github:acme/acme-mcp"
    assert candidates[0].summary == "Acme MCP server"


def test_github_candidate_to_minimal_draft() -> None:
    candidate = parse_search_response(
        {
            "items": [
                {
                    "full_name": "acme/acme-mcp",
                    "html_url": "https://github.com/acme/acme-mcp",
                    "description": "Acme MCP server",
                }
            ]
        }
    )[0]

    draft = candidate_to_minimal_draft(candidate)

    assert draft.name == "Acme Mcp"
    assert draft.platforms == ["mcp"]
    assert draft.listing_types == ["mcp-server"]
    assert draft.capabilities["open_source"] == "yes"
    assert draft.repo_url == "https://github.com/acme/acme-mcp"


def test_fetch_github_readme_text_uses_contents_api() -> None:
    calls = []

    def fake_get_json(url: str, params: dict, headers: dict) -> dict:
        calls.append({"url": url, "params": params, "headers": headers})
        return {
            "encoding": "base64",
            "content": base64.b64encode(b"# Acme MCP\nREADME body").decode("ascii"),
            "html_url": "https://github.com/acme/acme-mcp/blob/main/README.md",
            "download_url": "https://raw.githubusercontent.com/acme/acme-mcp/main/README.md",
            "path": "README.md",
            "size": 22,
        }

    result = fetch_github_readme_text(
        "https://github.com/acme/acme-mcp",
        token="gh-token",
        get_json=fake_get_json,
    )

    assert calls[0]["url"] == "https://api.github.com/repos/acme/acme-mcp/readme"
    assert calls[0]["headers"]["Authorization"] == "Bearer gh-token"
    assert result.content_type == "text/markdown"
    assert result.text == "# Acme MCP\nREADME body"
    assert result.raw_payload["source"] == "github_readme"
