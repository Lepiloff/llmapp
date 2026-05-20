"""Gemini Extensions direct-ingest source."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.utils.text import slugify

from apps.agent.pipeline.rate_limit import get_default_limiter
from apps.sources.base import AppDraft, BaseSource
from apps.sources.models import Source

logger = logging.getLogger(__name__)

FetchJson = Callable[[str], object]

USER_AGENT = "LLMAppMarket-Agent/1.0 (+https://llmappmarket.com/bots)"

_BOOL_CAPABILITIES = {
    "hasMCP": "gemini_has_mcp",
    "hasContext": "gemini_has_context",
    "hasHooks": "gemini_has_hooks",
    "hasSkills": "gemini_has_skills",
    "hasCustomCommands": "gemini_has_custom_commands",
}


class GeminiExtensionsSource(BaseSource):
    """Fetch and normalize the public Gemini CLI extensions JSON feed."""

    source_type = Source.SourceType.GEMINI_EXTENSIONS
    source_name = "gemini_extensions"

    def __init__(
        self,
        *,
        url: str | None = None,
        fetch_json: FetchJson | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url or settings.GEMINI_EXTENSIONS_URL
        self.fetch_json = fetch_json or self._requests_fetch_json
        self.timeout = timeout
        self.parse_failures: list[dict] = []

    def iter_drafts(self) -> Iterable[AppDraft]:
        try:
            payload = self.fetch_json(self.url)
        except requests.RequestException as exc:
            logger.exception("gemini_extensions_fetch_failed", extra={"url": self.url})
            _report_warning_to_sentry(
                "Gemini Extensions fetch failed",
                {"url": self.url, "error_class": type(exc).__name__},
            )
            return

        entries = _extract_entries(payload)
        if entries is None:
            logger.warning(
                "gemini_extensions_payload_invalid",
                extra={"received_type": type(payload).__name__},
            )
            return

        for rank, entry in enumerate(entries, start=1):
            try:
                yield _entry_to_draft(entry, rank=rank)
            except ValueError as exc:
                self.parse_failures.append({"entry": entry, "error": str(exc)})
                logger.warning(
                    "gemini_extensions_entry_skipped",
                    extra={"rank": rank, "error": str(exc)},
                )
                continue

        if entries and len(self.parse_failures) / len(entries) >= 0.10:
            _report_warning_to_sentry(
                "Gemini Extensions parse failure threshold exceeded",
                {
                    "total": len(entries),
                    "failed": len(self.parse_failures),
                    "url": self.url,
                },
            )

    def _requests_fetch_json(self, url: str) -> object:
        get_default_limiter().acquire(url)
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()


def _extract_entries(payload: object) -> list[dict] | None:
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get("extensions") or payload.get("items") or []
    else:
        return None
    if not isinstance(entries, list):
        return None
    return [entry for entry in entries if isinstance(entry, dict)]


def _entry_to_draft(entry: dict, *, rank: int) -> AppDraft:
    external_raw_id = str(entry.get("id") or "").strip()
    repo_url = str(entry.get("url") or "").strip()
    full_name = str(entry.get("fullName") or "").strip()
    extension_name = str(entry.get("extensionName") or "").strip()
    repo_name = full_name.split("/")[-1] if "/" in full_name else ""
    name = extension_name or repo_name

    if not external_raw_id:
        raise ValueError("missing id")
    if not name:
        raise ValueError("missing extensionName/fullName")

    developer_name = full_name.split("/")[0] if "/" in full_name else ""
    developer_url = f"https://github.com/{developer_name}" if developer_name else ""
    short_description = str(entry.get("extensionDescription") or "").strip()
    long_description = str(entry.get("repoDescription") or "").strip()
    platforms = ["gemini"]
    if bool(entry.get("hasMCP")):
        platforms.append("mcp")

    capabilities: dict[str, str] = {}
    evidence: dict[str, str] = {}
    for source_key, capability_key in _BOOL_CAPABILITIES.items():
        value = bool(entry.get(source_key))
        capabilities[capability_key] = "yes" if value else "no"
        evidence[capability_key] = (
            f"Gemini extension manifest declares {source_key}:{str(value).lower()}"
        )
    if _is_github_url(repo_url):
        capabilities["open_source"] = "yes"
        evidence["open_source"] = "Extension feed points to a public GitHub repository."

    metadata = {
        "extension_version": entry.get("extensionVersion") or "",
        "google_owned": bool(entry.get("isGoogleOwned")),
        "rank": rank,
        "last_updated": entry.get("lastUpdated") or "",
        "license_key": entry.get("licenseKey") or "",
        "stars": entry.get("stars"),
        "avatar_url": entry.get("avatarUrl") or "",
        "manifest_flags": {
            key: bool(entry.get(key)) for key in _BOOL_CAPABILITIES
        },
    }

    return AppDraft(
        name=name,
        slug_hint=slugify(name) or slugify(repo_name) or "gemini-extension",
        short_description=short_description[:280],
        long_description=long_description,
        developer_name=developer_name,
        developer_url=developer_url,
        official_page_url=repo_url,
        repo_url=repo_url,
        platforms=platforms,
        listing_types=["gemini-extension"],
        capabilities=capabilities,
        capability_evidence=evidence,
        external_id=f"gemini:{external_raw_id}",
        raw_payload=dict(entry),
        platform_metadata=metadata,
    )


def _is_github_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower() in {"github.com", "www.github.com"}


def _report_warning_to_sentry(message: str, context: dict) -> None:
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover - sentry-sdk is a soft runtime hook
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", "gemini_extensions_ingest")
        scope.set_context("gemini_extensions", context)
        sentry_sdk.capture_message(message, level="warning")
