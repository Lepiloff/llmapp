"""RSS / Atom discovery source.

The source intentionally uses the Python standard library parser here.
It keeps Phase 3 discovery testable without adding a runtime dependency
that deployments have not installed yet. The parser covers the subset
we need from normal RSS 2.0 and Atom feeds: title, link, summary, and
published timestamp.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable

import requests

from .base import DiscoveryCandidate


DEFAULT_RSS_FEEDS: tuple[str, ...] = (
    "https://openai.com/news/rss.xml",
    "https://www.anthropic.com/news/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://github.com/topics/mcp-server.atom",
)

FetchText = Callable[[str], str]


class RSSFeedSource:
    """Fetch configured RSS/Atom feeds and yield normalized candidates."""

    source_name = "rss"

    def __init__(
        self,
        *,
        feed_urls: Iterable[str] = DEFAULT_RSS_FEEDS,
        fetch_text: FetchText | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.feed_urls = tuple(feed_urls)
        self.fetch_text = fetch_text or self._requests_fetch
        self.timeout = timeout

    def iter_candidates(self, *, limit: int | None = None) -> Iterable[DiscoveryCandidate]:
        yielded = 0
        for feed_url in self.feed_urls:
            text = self.fetch_text(feed_url)
            for candidate in parse_feed(text, feed_url=feed_url):
                yield candidate
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    def _requests_fetch(self, url: str) -> str:
        resp = requests.get(
            url,
            timeout=self.timeout,
            headers={"User-Agent": "LLMAppMarket-Agent/1.0 (+https://llmappmarket.com/bots)"},
        )
        resp.raise_for_status()
        return resp.text


def parse_feed(xml_text: str, *, feed_url: str) -> list[DiscoveryCandidate]:
    """Parse RSS 2.0 or Atom XML into discovery candidates."""
    root = ET.fromstring(xml_text)
    candidates: list[DiscoveryCandidate] = []

    rss_items = root.findall(".//item")
    if rss_items:
        for item in rss_items:
            title = _text(item, "title")
            link = _text(item, "link") or _text(item, "guid")
            summary = _text(item, "description")
            published = _text(item, "pubDate")
            if link:
                candidates.append(
                    _candidate(
                        feed_url=feed_url,
                        url=link,
                        title=title or link,
                        summary=summary,
                        published=published,
                    )
                )
        return candidates

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//atom:entry", ns):
        title = _text(entry, "atom:title", ns)
        link = ""
        link_el = entry.find("atom:link", ns)
        if link_el is not None:
            link = link_el.attrib.get("href", "")
        summary = _text(entry, "atom:summary", ns) or _text(entry, "atom:content", ns)
        published = _text(entry, "atom:published", ns) or _text(entry, "atom:updated", ns)
        if link:
            candidates.append(
                _candidate(
                    feed_url=feed_url,
                    url=link,
                    title=title or link,
                    summary=summary,
                    published=published,
                )
            )
    return candidates


def _candidate(
    *,
    feed_url: str,
    url: str,
    title: str,
    summary: str,
    published: str,
) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        external_id=f"rss:{url}",
        url=url.strip(),
        title=_clean(title),
        summary=_clean(summary),
        source_name="rss",
        raw_payload={
            "feed_url": feed_url,
            "url": url,
            "title": title,
            "summary": summary,
            "published": published,
        },
    )


def _text(el: ET.Element, path: str, ns: dict | None = None) -> str:
    child = el.find(path, ns or {})
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _clean(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
