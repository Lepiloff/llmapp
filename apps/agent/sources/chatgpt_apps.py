"""ChatGPT Apps direct-ingest source backed by a crawlable third-party index."""
from __future__ import annotations

import itertools
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, FeatureNotFound
from django.conf import settings
from django.utils.text import slugify

from apps.agent.pipeline.rate_limit import get_default_limiter
from apps.sources.base import AppDraft, BaseSource
from apps.sources.models import Source

logger = logging.getLogger(__name__)

FetchText = Callable[[str], str]

USER_AGENT = "LLMAppMarket-Agent/1.0 (+https://llmappmarket.com/bots)"
_ROBOTS_TTL_SECONDS = 3600
_ROBOTS_CACHE: dict[str, tuple[float, bool]] = {}

_CATEGORY_MAP = {
    "ai/ml": "developer-tools",
    "automotive": "commerce",
    "business": "productivity",
    "commerce": "commerce",
    "data": "data-analytics",
    "data & analytics": "data-analytics",
    "design": "design",
    "education": "research",
    "featured": "",
    "files": "files",
    "finance": "commerce",
    "food": "commerce",
    "health": "research",
    "local": "travel",
    "marketing": "marketing",
    "music": "design",
    "news": "research",
    "productivity": "productivity",
    "real estate": "travel",
    "research": "research",
    "shopping": "commerce",
    "travel": "travel",
}


class ChatGPTAppsSource(BaseSource):
    """Crawl mcpapp.net's public ChatGPT Apps index into normalized drafts.

    This is intentionally not treated as an official OpenAI directory feed.
    The source is useful for MVP discovery, while editorial review remains the
    trust boundary before anything becomes public.
    """

    source_type = Source.SourceType.CHATGPT_UNOFFICIAL
    source_name = "chatgpt_apps"

    def __init__(
        self,
        *,
        index_url: str | None = None,
        fetch_text: FetchText | None = None,
        timeout: float = 30.0,
        max_pages: int | None = None,
    ) -> None:
        self.index_url = (index_url or settings.CHATGPT_APPS_INDEX_URL).rstrip("/")
        self.fetch_text = fetch_text or self._requests_fetch_text
        self.timeout = timeout
        self.max_pages = max_pages
        self.parse_failures: list[dict] = []

    def iter_drafts(self) -> Iterable[AppDraft]:
        if not self._robots_allows():
            logger.warning("chatgpt_apps_robots_disallow", extra={"url": self.index_url})
            return

        seen_urls: set[str] = set()
        page_numbers = (
            itertools.count(1)
            if self.max_pages is None
            else range(1, self.max_pages + 1)
        )
        for page in page_numbers:
            page_url = self._page_url(page)
            try:
                index_html = self.fetch_text(page_url)
            except requests.RequestException:
                logger.exception("chatgpt_apps_index_fetch_failed", extra={"url": page_url})
                return

            cards = parse_index_page(index_html, base_url=self.index_url)
            if not cards:
                break

            new_cards = []
            for card in cards:
                detail_url = card.get("detail_url") or ""
                if not detail_url or detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_cards.append(card)

            if not new_cards:
                logger.info(
                    "chatgpt_apps_no_new_cards",
                    extra={"url": page_url, "page": page},
                )
                break

            for card in new_cards:
                detail_url = card.get("detail_url") or ""
                try:
                    detail_html = self.fetch_text(detail_url)
                    detail = parse_detail_page(detail_html, detail_url=detail_url)
                    yield card_to_draft(card, detail, detail_url=detail_url)
                except (requests.RequestException, ValueError) as exc:
                    self.parse_failures.append({"url": detail_url, "error": str(exc)})
                    logger.warning(
                        "chatgpt_apps_detail_skipped",
                        extra={"url": detail_url, "error": str(exc)},
                    )
                    continue

            if not has_next_page(index_html, current_page=page):
                break

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self.index_url
        separator = "&" if "?" in self.index_url else "?"
        return f"{self.index_url}{separator}page={page}"

    def _requests_fetch_text(self, url: str) -> str:
        get_default_limiter().acquire(url)
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    def _robots_allows(self) -> bool:
        parsed = urlparse(self.index_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        now = time.time()
        cached = _ROBOTS_CACHE.get(robots_url)
        if cached and now - cached[0] < _ROBOTS_TTL_SECONDS:
            return cached[1]

        try:
            robots_text = self.fetch_text(robots_url)
        except requests.RequestException:
            _ROBOTS_CACHE[robots_url] = (now, False)
            return False

        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(robots_text.splitlines())
        allowed = parser.can_fetch(USER_AGENT, self.index_url)
        _ROBOTS_CACHE[robots_url] = (now, allowed)
        return allowed


def parse_index_page(html: str, *, base_url: str) -> list[dict]:
    soup = _soup(html)
    cards = _cards_from_html(soup, base_url=base_url)
    if not cards:
        cards = _cards_from_json_ld(soup, base_url=base_url)
    return _dedupe_cards(cards)


def parse_detail_page(html: str, *, detail_url: str) -> dict:
    soup = _soup(html)
    info = _info_table(soup)
    json_ld = _software_application_json_ld(soup)
    name = _text(soup.select_one("h1")) or str(json_ld.get("name") or "")
    tagline = _text(soup.select_one(".app-head-text p"))
    meta_description = _meta_content(soup, "description")
    long_description = _first_detail_paragraph(soup) or str(json_ld.get("description") or "")
    connect_url = _connect_url(soup) or _first_surface_url(info.get("available in", ""))
    categories = _category_labels(soup, info.get("category", ""))
    capabilities = _split_labels(info.get("capabilities", ""))
    developer_name = info.get("developer", "")
    developer_url = _first_url(info.get("website", "")) or _author_url(json_ld)

    return {
        "name": name.strip(),
        "short_description": tagline.strip(),
        "long_description": long_description.strip() or meta_description.strip(),
        "developer_name": developer_name.strip() or _author_name(json_ld),
        "developer_url": developer_url.strip(),
        "connect_url": connect_url.strip(),
        "categories": categories,
        "capability_labels": capabilities,
        "auth": info.get("auth", ""),
        "transport": info.get("transport", ""),
        "version": info.get("version", ""),
        "privacy_url": _first_url(info.get("privacy policy", "")),
        "terms_url": _first_url(info.get("terms of service", "")),
        "support_url": _first_url(info.get("customer support", "")),
        "json_ld": json_ld,
    }


def card_to_draft(card: dict, detail: dict, *, detail_url: str) -> AppDraft:
    name = (detail.get("name") or card.get("name") or "").strip()
    slug = _slug_from_detail_url(detail_url) or slugify(name)
    if not name:
        raise ValueError("missing app name")
    if not slug:
        raise ValueError("missing app slug")

    source_categories = list(detail.get("categories") or card.get("categories") or [])
    categories, unmapped = _map_categories(source_categories)
    platforms, listing_types = _platforms_and_listing_types(card, detail)
    capabilities, evidence = _capabilities_from_detail(detail)
    connect_url = detail.get("connect_url") or ""
    official_page_url = connect_url or detail_url
    developer_url = detail.get("developer_url") or ""

    raw_payload = {
        "source": "mcpapp.net",
        "source_kind": "third_party_chatgpt_apps_index",
        "card": card,
        "detail": detail,
        "unmapped_categories": unmapped,
    }
    metadata = {
        "mcpapp_detail_url": detail_url,
        "connect_url": connect_url,
        "version": detail.get("version") or "",
        "auth": detail.get("auth") or "",
        "transport": detail.get("transport") or "",
        "privacy_url": detail.get("privacy_url") or "",
        "terms_url": detail.get("terms_url") or "",
        "support_url": detail.get("support_url") or "",
        "surface_labels": list(card.get("surface_labels") or []),
        "icon_url": card.get("icon_url") or "",
    }

    return AppDraft(
        name=name,
        slug_hint=slug,
        short_description=(detail.get("short_description") or card.get("short_description") or "")[:280],
        long_description=detail.get("long_description") or "",
        developer_name=detail.get("developer_name") or "",
        developer_url=developer_url,
        official_page_url=official_page_url,
        install_url=connect_url,
        platforms=platforms,
        listing_types=listing_types,
        categories=categories,
        capabilities=capabilities,
        capability_evidence=evidence,
        use_cases=_use_cases_from(source_categories, detail.get("capability_labels") or []),
        external_id=f"mcpapp-chatgpt:{slug}",
        raw_payload=raw_payload,
        platform_metadata=metadata,
        official_directory_url=connect_url,
    )


def has_next_page(html: str, *, current_page: int) -> bool:
    soup = _soup(html)
    next_page = str(current_page + 1)
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        if f"page={next_page}" in href:
            return True
        if link.get_text(" ", strip=True).lower() == "next":
            return True
    return False


def _cards_from_html(soup: BeautifulSoup, *, base_url: str) -> list[dict]:
    cards: list[dict] = []
    for node in soup.select("a.app-row[href^='/app/'], a.app-row[href*='/app/']"):
        href = str(node.get("href") or "")
        detail_url = urljoin(base_url + "/", href)
        name = _text(node.select_one(".app-name"))
        for hidden in node.select(".surface-badges"):
            name = name.replace(_text(hidden), "").strip()
        short_description = _text(node.select_one(".app-tag"))
        surfaces = [
            str(item.get("title") or item.get("aria-label") or "").strip()
            for item in node.select(".surface-badge")
            if str(item.get("title") or item.get("aria-label") or "").strip()
        ]
        if not name or not detail_url:
            continue
        cards.append(
            {
                "name": name,
                "slug": _slug_from_detail_url(detail_url),
                "detail_url": detail_url,
                "short_description": short_description,
                "surface_labels": surfaces,
                "icon_url": _attr_src(node, "img"),
            }
        )
    return cards


def _cards_from_json_ld(soup: BeautifulSoup, *, base_url: str) -> list[dict]:
    cards: list[dict] = []
    for payload in _json_ld_payloads(soup):
        for obj in _as_json_ld_list(payload):
            if obj.get("@type") != "ItemList":
                continue
            for item in obj.get("itemListElement") or []:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or item.get("item") or "")
                name = str(item.get("name") or "").strip()
                if "/app/" not in url or not name:
                    continue
                detail_url = urljoin(base_url + "/", url)
                cards.append(
                    {
                        "name": name,
                        "slug": _slug_from_detail_url(detail_url),
                        "detail_url": detail_url,
                        "short_description": "",
                        "surface_labels": ["ChatGPT App"],
                    }
                )
    return cards


def _dedupe_cards(cards: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for card in cards:
        key = card.get("detail_url") or card.get("slug") or card.get("name")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(card)
    return result


def _platforms_and_listing_types(card: dict, detail: dict) -> tuple[list[str], list[str]]:
    surfaces = " ".join(card.get("surface_labels") or []).lower()
    platforms = ["chatgpt"]
    listing_types = ["chatgpt-app"]
    if "claude" in surfaces:
        platforms.append("claude")
        listing_types.append(
            "interactive-claude-app" if "interactive" in surfaces else "claude-connector"
        )
    return sorted(set(platforms)), sorted(set(listing_types))


def _capabilities_from_detail(detail: dict) -> tuple[dict[str, str], dict[str, str]]:
    labels = {label.lower() for label in detail.get("capability_labels") or []}
    auth = str(detail.get("auth") or "").strip().lower()
    transport = str(detail.get("transport") or "").strip().lower()
    capabilities: dict[str, str] = {}
    evidence: dict[str, str] = {}
    if "interactive" in labels:
        capabilities["interactive_ui"] = "yes"
        evidence["interactive_ui"] = "mcpapp.net lists the app capability as Interactive."
    if "writes" in labels or "write" in labels:
        capabilities["write_actions"] = "yes"
        evidence["write_actions"] = "mcpapp.net lists the app capability as Writes."
    if "reads" in labels or "read" in labels:
        capabilities["read_data"] = "yes"
        evidence["read_data"] = "mcpapp.net lists the app capability as Reads."
    if auth and auth not in {"none", "not provided"}:
        capabilities["auth_required"] = "yes"
        evidence["auth_required"] = f"mcpapp.net lists auth as {auth}."
    elif auth == "none":
        capabilities["auth_required"] = "no"
        evidence["auth_required"] = "mcpapp.net lists auth as none."
    if transport in {"http", "sse", "remote"}:
        capabilities["remote_available"] = "yes"
        evidence["remote_available"] = f"mcpapp.net lists transport as {transport}."
    return capabilities, evidence


def _map_categories(source_categories: list[str]) -> tuple[list[str], list[str]]:
    mapped: list[str] = []
    unmapped: list[str] = []
    for category in source_categories:
        normalized = _normalize_label(category)
        slug = _CATEGORY_MAP.get(normalized)
        if slug:
            mapped.append(slug)
        elif category and normalized != "featured":
            unmapped.append(category)
    return sorted(set(mapped)), unmapped


def _use_cases_from(categories: list[str], capability_labels: list[str]) -> list[str]:
    use_cases = [f"Use in {category} workflows" for category in categories if category.lower() != "featured"]
    use_cases.extend(f"Review {label.lower()} capability" for label in capability_labels)
    return use_cases[:7]


def _info_table(soup: BeautifulSoup) -> dict[str, str]:
    info: dict[str, str] = {}
    for row in soup.select(".info-row"):
        key = _text(row.select_one(".info-key")).lower()
        value_node = row.select_one(".info-val")
        if key and value_node:
            links = [
                str(link.get("href") or "").strip()
                for link in value_node.find_all("a", href=True)
                if str(link.get("href") or "").strip()
            ]
            text = value_node.get_text(" ", strip=True)
            info[key] = " ".join([text, *links]).strip()
    return info


def _category_labels(soup: BeautifulSoup, fallback: str) -> list[str]:
    labels: list[str] = []
    for link in soup.select(".info-row a[href^='/category/'], .crumbs a[href^='/category/']"):
        text = _text(link)
        if text:
            labels.append(text)
    if not labels:
        labels = _split_labels(fallback)
    return labels


def _first_detail_paragraph(soup: BeautifulSoup) -> str:
    for section in soup.select(".detail-section.prose-grid, .detail-section"):
        paragraph = section.select_one("p")
        text = _text(paragraph)
        if text and not text.lower().startswith("about "):
            return text
    return ""


def _connect_url(soup: BeautifulSoup) -> str:
    href = _attr_href(soup, ".btn-connect")
    if href:
        return href
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        if "chatgpt.com/apps/" in href:
            return href
    return ""


def _software_application_json_ld(soup: BeautifulSoup) -> dict:
    for payload in _json_ld_payloads(soup):
        for obj in _as_json_ld_list(payload):
            if obj.get("@type") == "SoftwareApplication":
                return obj
    return {}


def _json_ld_payloads(soup: BeautifulSoup) -> Iterable[object]:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text()
        if not text:
            continue
        try:
            yield json.loads(text)
        except ValueError:
            continue


def _as_json_ld_list(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [obj for obj in payload if isinstance(obj, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _author_name(json_ld: dict) -> str:
    author = json_ld.get("author")
    return str(author.get("name") or "").strip() if isinstance(author, dict) else ""


def _author_url(json_ld: dict) -> str:
    author = json_ld.get("author")
    return str(author.get("url") or "").strip() if isinstance(author, dict) else ""


def _first_surface_url(value: str) -> str:
    return _first_url(value)


def _first_url(value: str) -> str:
    match = re.search(r"https?://[^\s)]+", value or "")
    return match.group(0) if match else ""


def _split_labels(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r",|\|", value) if part.strip()]


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html, "html.parser")


def _text(node) -> str:
    if node is None:
        return ""
    return node.get_text(" ", strip=True)


def _attr_href(node, selector: str) -> str:
    match = node.select_one(selector)
    if match is None:
        return ""
    return str(match.get("href") or "").strip()


def _attr_src(node, selector: str) -> str:
    match = node.select_one(selector)
    if match is None:
        return ""
    return str(match.get("src") or "").strip()


def _meta_content(soup: BeautifulSoup, name: str) -> str:
    match = soup.select_one(f"meta[name='{name}']")
    if match is None:
        return ""
    return str(match.get("content") or "").strip()


def _slug_from_detail_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return ""
    return parts[-1]


def _normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())
