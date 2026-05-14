"""GitHub MCP repository discovery source."""
from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterable
from urllib.parse import urlparse

import requests
from django.utils.text import slugify

from apps.agent.pipeline.fetch import FetchResult
from apps.sources.base import AppDraft

from .base import DiscoveryCandidate


GitHubGet = Callable[[str, dict, dict], dict]


class GitHubMCPSearchSource:
    """Search GitHub for recently active MCP server repositories."""

    source_name = "github_mcp"

    def __init__(
        self,
        *,
        token: str = "",
        query: str = "topic:mcp-server stars:>5",
        get_json: GitHubGet | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.token = token
        self.query = query
        self.get_json = get_json or self._requests_get_json
        self.timeout = timeout

    def iter_candidates(self, *, limit: int | None = None) -> Iterable[DiscoveryCandidate]:
        payload = self.get_json(
            "https://api.github.com/search/repositories",
            {"q": self.query, "sort": "updated", "order": "desc", "per_page": limit or 20},
            self._headers(),
        )
        candidates = parse_search_response(payload)
        yield from candidates[:limit]

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "LLMAppMarket-Agent/1.0 (+https://llmappmarket.com/bots)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _requests_get_json(self, url: str, params: dict, headers: dict) -> dict:
        resp = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


def parse_search_response(payload: dict) -> list[DiscoveryCandidate]:
    candidates: list[DiscoveryCandidate] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        full_name = item.get("full_name") or ""
        html_url = item.get("html_url") or ""
        if not full_name or not html_url:
            continue
        description = item.get("description") or ""
        candidates.append(
            DiscoveryCandidate(
                external_id=f"github:{full_name.lower()}",
                url=html_url,
                title=full_name,
                summary=description,
                source_name="github_mcp",
                raw_payload={
                    "id": item.get("id"),
                    "full_name": full_name,
                    "html_url": html_url,
                    "description": description,
                    "stargazers_count": item.get("stargazers_count"),
                    "pushed_at": item.get("pushed_at"),
                    "language": item.get("language"),
                    "owner": item.get("owner") or {},
                },
            )
        )
    return candidates


def candidate_to_minimal_draft(candidate: DiscoveryCandidate) -> AppDraft:
    """Build a conservative DRAFT from GitHub search metadata.

    This is intentionally sparse. Rich README parsing / LLM enrichment is
    still handled by later Phase 3 work; this function only gives editors
    a safe draft when an operator explicitly runs discovery with
    ``dry_run=False``.
    """
    repo_name = candidate.title.split("/")[-1] or candidate.title
    return AppDraft(
        name=repo_name.replace("-", " ").replace("_", " ").strip().title(),
        slug_hint=slugify(repo_name) or "github-mcp",
        short_description=candidate.summary[:280],
        repo_url=candidate.url,
        official_page_url=candidate.url,
        platforms=["mcp"],
        listing_types=["mcp-server"],
        capabilities={"open_source": "yes"},
        external_id=candidate.external_id,
        raw_payload=candidate.raw_payload,
        platform_metadata={
            "discovered_via": "github_search",
            "repository_url": candidate.url,
        },
    )


def fetch_github_readme_text(
    repo_url: str,
    *,
    token: str = "",
    get_json: GitHubGet | None = None,
) -> FetchResult:
    """Fetch README content through the GitHub Contents API.

    The LLM gets cleaner signal from README markdown than from GitHub's
    HTML repository page. This function is intentionally test-injectable:
    production uses authenticated `requests`, tests pass `get_json`.
    """
    full_name = _repo_full_name_from_url(repo_url)
    if not full_name:
        raise ValueError(f"Not a GitHub repository URL: {repo_url!r}")
    api_url = f"https://api.github.com/repos/{full_name}/readme"
    getter = get_json or _requests_get_json_with_token(token=token)
    payload = getter(
        api_url,
        {},
        {
            "Accept": "application/vnd.github+json",
            "User-Agent": "LLMAppMarket-Agent/1.0 (+https://llmappmarket.com/bots)",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    text = _decode_readme_payload(payload)
    return FetchResult(
        url=repo_url,
        final_url=payload.get("html_url") or repo_url,
        status_code=200,
        content_type="text/markdown",
        text=text,
        raw_payload={
            "source": "github_readme",
            "repo_url": repo_url,
            "api_url": api_url,
            "html_url": payload.get("html_url") or "",
            "download_url": payload.get("download_url") or "",
            "path": payload.get("path") or "",
            "size": payload.get("size") or 0,
        },
    )


def _requests_get_json_with_token(*, token: str) -> GitHubGet:
    def _get(url: str, params: dict, headers: dict) -> dict:
        resp = requests.get(url, params=params, headers=headers, timeout=15.0)
        resp.raise_for_status()
        return resp.json()

    return _get


def _repo_full_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return ""
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return ""
    return f"{parts[0]}/{parts[1]}"


def _decode_readme_payload(payload: dict) -> str:
    encoding = (payload.get("encoding") or "").lower()
    content = payload.get("content") or ""
    if encoding != "base64" or not content:
        raise ValueError("GitHub README payload is missing base64 content.")
    try:
        raw = base64.b64decode(content, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("GitHub README payload contains invalid base64.") from exc
    return raw.decode("utf-8", errors="replace")
