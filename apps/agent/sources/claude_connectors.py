"""Claude Connectors direct-ingest source."""
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

_ANTHROPIC_CATEGORY_MAP = {
    "ai/ml": "developer-tools",
    "automation": "productivity",
    "calendar": "productivity",
    "cloud": "developer-tools",
    "cms": "marketing",
    "customer support": "sales-crm",
    "data & analytics": "data-analytics",
    "design": "design",
    "desktop automation": "productivity",
    "development tools": "developer-tools",
    "documents": "files",
    "education": "research",
    "finance": "commerce",
    "jobs": "productivity",
    "lifestyle": "productivity",
    "marketing": "marketing",
    "observability": "developer-tools",
    "productivity": "productivity",
    "project management": "productivity",
    "research": "research",
    "security": "developer-tools",
    "seo": "marketing",
    "ticketing": "productivity",
    "travel": "travel",
}


class ClaudeConnectorsSource(BaseSource):
    """Crawl public Claude Connector pages into normalized drafts."""

    source_type = Source.SourceType.CLAUDE_CONNECTORS
    source_name = "claude_connectors"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        fetch_text: FetchText | None = None,
        timeout: float = 30.0,
        max_pages: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.CLAUDE_CONNECTORS_BASE_URL).rstrip("/")
        self.fetch_text = fetch_text or self._requests_fetch_text
        self.timeout = timeout
        self.max_pages = max_pages
        self.parse_failures: list[dict] = []

    def iter_drafts(self) -> Iterable[AppDraft]:
        if not self._robots_allows():
            logger.warning("claude_connectors_robots_disallow", extra={"url": self.base_url})
            _report_warning_to_sentry(
                "Claude Connectors crawl blocked by robots.txt",
                {"base_url": self.base_url},
            )
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
                logger.exception("claude_connectors_index_fetch_failed", extra={"url": page_url})
                return

            cards = parse_index_page(index_html, base_url=self.base_url)
            if not cards:
                break

            for card in cards:
                detail_url = card.get("detail_url") or ""
                if not detail_url or detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                try:
                    detail_html = self.fetch_text(detail_url)
                    detail = parse_detail_page(detail_html, detail_url=detail_url)
                    yield card_to_draft(card, detail, detail_url=detail_url)
                except (requests.RequestException, ValueError) as exc:
                    self.parse_failures.append({"url": detail_url, "error": str(exc)})
                    logger.warning(
                        "claude_connectors_detail_skipped",
                        extra={"url": detail_url, "error": str(exc)},
                    )
                    continue

            if not has_next_page(index_html, current_page=page, base_url=self.base_url):
                break

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self.base_url
        separator = "&" if "?" in self.base_url else "?"
        return f"{self.base_url}{separator}page={page}"

    def _requests_fetch_text(self, url: str) -> str:
        get_default_limiter().acquire(url)
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    def _robots_allows(self) -> bool:
        parsed = urlparse(self.base_url)
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
        allowed = parser.can_fetch(USER_AGENT, self.base_url)
        _ROBOTS_CACHE[robots_url] = (now, allowed)
        return allowed


def parse_index_page(html: str, *, base_url: str) -> list[dict]:
    soup = _soup(html)
    cards = _cards_from_html(soup, base_url=base_url)
    if not cards:
        cards = _cards_from_json_scripts(soup, base_url=base_url)
    return _dedupe_cards(cards)


def parse_detail_page(html: str, *, detail_url: str) -> dict:
    soup = _soup(html)
    main = soup.select_one("main") or soup
    long_description = _attr_text(main, "data-long-description") or _paragraph_text(main)
    main_text = main.get_text(" ", strip=True)
    developer_name = (
        _attr_text(main, "data-developer-name")
        or _developer_from_made_by(main_text)
        or _developer_from_text(main_text)
    )
    developer_url = _external_url_or_empty(
        _attr_href(main, "[data-developer-url]") or _link_by_label(
            main, ("developer", "website")
        ),
        base_url=detail_url,
    )
    official_candidate = _attr_href(main, "[data-official-url]") or _link_by_label(
        main, ("connect", "install", "get started")
    )
    official_url = _external_url_or_empty(official_candidate, base_url=detail_url) or detail_url
    capabilities, evidence = _capabilities_from_text(main_text)
    return {
        "name": _text(main.select_one("h1")),
        "long_description": long_description,
        "developer_name": developer_name,
        "developer_url": developer_url,
        "official_url": official_url or detail_url,
        "capabilities": capabilities,
        "capability_evidence": evidence,
    }


def card_to_draft(card: dict, detail: dict, *, detail_url: str) -> AppDraft:
    name = (detail.get("name") or card.get("name") or "").strip()
    slug = card.get("slug") or slugify(name)
    if not name:
        raise ValueError("missing connector name")
    if not slug:
        raise ValueError("missing connector slug")

    source_categories = list(card.get("categories") or [])
    categories, unmapped = _map_categories(source_categories)
    raw_payload = {
        "card": card,
        "detail": detail,
        "unmapped_categories": unmapped,
    }
    metadata = {
        "compatible_products": list(card.get("compatible_products") or []),
        "use_case_categories": source_categories,
        "publication_date": card.get("publication_date") or "",
        "logo_url": card.get("logo_url") or "",
    }

    return AppDraft(
        name=name,
        slug_hint=slug,
        short_description=(card.get("short_description") or "")[:280],
        long_description=detail.get("long_description") or "",
        developer_name=detail.get("developer_name") or "",
        developer_url=detail.get("developer_url") or "",
        official_page_url=detail.get("official_url") or detail_url,
        platforms=["claude"],
        listing_types=["claude-connector"],
        categories=categories,
        capabilities=dict(detail.get("capabilities") or {}),
        capability_evidence=dict(detail.get("capability_evidence") or {}),
        use_cases=source_categories,
        external_id=f"claude:{slug}",
        raw_payload=raw_payload,
        platform_metadata=metadata,
        official_directory_url=detail_url,
    )


def has_next_page(html: str, *, current_page: int, base_url: str) -> bool:
    soup = _soup(html)
    next_page = str(current_page + 1)
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        if f"page={next_page}" in href:
            return True
        if link.get_text(" ", strip=True).lower() in {"next", "next page"}:
            return True
    return False


def _cards_from_html(soup: BeautifulSoup, *, base_url: str) -> list[dict]:
    candidates = soup.select(
        "[data-connector-card], [data-testid='connector-card'], article, .stories_cms_item"
    )
    cards: list[dict] = []
    for node in candidates:
        link = node.select_one("a[href*='/connectors/']")
        href = str(link.get("href", "")) if link else ""
        detail_url = urljoin(base_url + "/", href) if href else ""
        if not _is_connector_detail_url(detail_url, base_url=base_url):
            continue
        slug = _slug_from_detail_url(detail_url)
        name = (
            _attr_text(node, "data-name")
            or _text(node.select_one("h2"))
            or _text(node.select_one("h3"))
            or _text(link)
        )
        if not detail_url or not name:
            continue
        cards.append(
            {
                "name": name,
                "slug": slug,
                "detail_url": detail_url,
                "short_description": _attr_text(node, "data-description")
                or _text(node.select_one("p")),
                "categories": _split_attr(node.get("data-categories"))
                or _field_texts(node, "usecase")
                or _texts(node.select("[data-category], .tag, .badge")),
                "compatible_products": _split_attr(node.get("data-products"))
                or _field_texts(node, "works-with"),
                "publication_date": str(node.get("data-publication-date") or "")
                or (_field_texts(node, "date") or [""])[0],
                "logo_url": _attr_src(node, "img"),
            }
        )
    return cards


def _cards_from_json_scripts(soup: BeautifulSoup, *, base_url: str) -> list[dict]:
    cards: list[dict] = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text or "connector" not in text.lower():
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        for obj in _walk_dicts(payload):
            card = _json_obj_to_card(obj, base_url=base_url)
            if card:
                cards.append(card)
    return cards


def _json_obj_to_card(obj: dict, *, base_url: str) -> dict | None:
    name = str(obj.get("name") or obj.get("title") or "").strip()
    href = str(obj.get("url") or obj.get("href") or obj.get("path") or "").strip()
    slug = str(obj.get("slug") or "").strip()
    if not href and slug:
        href = f"/connectors/{slug}"
    if not name or "/connectors/" not in href:
        return None
    detail_url = urljoin(base_url + "/", href)
    if not _is_connector_detail_url(detail_url, base_url=base_url):
        return None
    return {
        "name": name,
        "slug": slug or _slug_from_detail_url(detail_url),
        "detail_url": detail_url,
        "short_description": str(obj.get("description") or obj.get("summary") or ""),
        "categories": _as_list(obj.get("categories") or obj.get("tags")),
        "compatible_products": _as_list(
            obj.get("compatibleProducts") or obj.get("compatible_products")
        ),
        "publication_date": str(obj.get("publicationDate") or obj.get("publishedAt") or ""),
        "logo_url": str(obj.get("logoUrl") or obj.get("logo_url") or ""),
    }


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


def _map_categories(source_categories: list[str]) -> tuple[list[str], list[str]]:
    mapped: list[str] = []
    unmapped: list[str] = []
    for category in source_categories:
        normalized = _normalize_label(category)
        slug = _ANTHROPIC_CATEGORY_MAP.get(normalized)
        if slug:
            mapped.append(slug)
        elif category:
            unmapped.append(category)
    return sorted(set(mapped)), unmapped


def _capabilities_from_text(text: str) -> tuple[dict[str, str], dict[str, str]]:
    lowered = text.lower()
    capabilities: dict[str, str] = {}
    evidence: dict[str, str] = {}
    if "read" in lowered:
        capabilities["read_data"] = "yes"
        evidence["read_data"] = "Claude Connector detail page mentions read access."
    if "write" in lowered or "create" in lowered or "update" in lowered:
        capabilities["write_actions"] = "yes"
        evidence["write_actions"] = "Claude Connector detail page mentions write actions."
    if "oauth" in lowered or "authorize" in lowered or "sign in" in lowered:
        capabilities["auth_required"] = "yes"
        evidence["auth_required"] = "Claude Connector detail page mentions authorization."
    return capabilities, evidence


def _field_texts(node, field: str) -> list[str]:
    return _texts(node.select(f"[fs-list-field='{field}']"))


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html, "html.parser")


def _walk_dicts(value) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _text(node) -> str:
    if node is None:
        return ""
    return node.get_text(" ", strip=True)


def _texts(nodes) -> list[str]:
    return [text for node in nodes if (text := _text(node))]


def _attr_text(node, attr: str) -> str:
    if node is None:
        return ""
    return str(node.get(attr) or "").strip()


def _attr_href(node, selector: str) -> str:
    match = node.select_one(selector)
    if match is None:
        return ""
    return str(match.get("href") or match.get("data-developer-url") or "").strip()


def _attr_src(node, selector: str) -> str:
    match = node.select_one(selector)
    if match is None:
        return ""
    return str(match.get("src") or "").strip()


def _paragraph_text(node) -> str:
    paragraphs = [_text(p) for p in node.select("p")]
    paragraphs = [p for p in paragraphs if p]
    return "\n\n".join(paragraphs[:3])


def _developer_from_text(text: str) -> str:
    match = re.search(r"\bby\s+([A-Z][\w .,&-]{1,80})", text)
    return match.group(1).strip() if match else ""


def _developer_from_made_by(text: str) -> str:
    match = re.search(r"\bMade by\s+(.+?)(?:\s+Play video|\s+Capabilities|\s+Category|$)", text)
    return match.group(1).strip() if match else ""


def _link_by_label(node, labels: tuple[str, ...]) -> str:
    for link in node.find_all("a", href=True):
        label = link.get_text(" ", strip=True).lower()
        if any(token in label for token in labels):
            return str(link["href"]).strip()
    return ""


def _slug_from_detail_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return ""
    return parts[-1]


def _is_connector_detail_url(url: str, *, base_url: str) -> bool:
    parsed = urlparse(url)
    base = urlparse(base_url)
    if parsed.scheme and parsed.netloc and parsed.netloc.lower() != base.netloc.lower():
        return False
    parts = [part for part in parsed.path.split("/") if part]
    return len(parts) >= 2 and parts[0] == "connectors" and parts[-1] != "connectors"


def _external_url_or_empty(url: str, *, base_url: str) -> str:
    parsed = urlparse(url)
    base = urlparse(base_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() != base.netloc.lower():
        return url
    return ""


def _split_attr(value) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return _split_attr(value)


def _normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _report_warning_to_sentry(message: str, context: dict) -> None:
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover - sentry-sdk is a soft runtime hook
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", "claude_connectors_ingest")
        scope.set_context("claude_connectors", context)
        sentry_sdk.capture_message(message, level="warning")
