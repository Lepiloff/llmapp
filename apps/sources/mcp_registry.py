"""MCP Registry ingest source.

Architecture refs:
  * docs/architecture.md § 9.2 (MCPRegistrySource)
  * docs/business.md § 13.4 (preview-status caveats)

Operational guarantees:
  * HTTP failures of one page stop the batch cleanly; we never re-raise into
    Celery so we don't put the worker into a crash loop.
  * Schema mismatches buffer the offending row into ``self.unparsed`` and
    the task persists them to ``UnparsedRegistryRecord`` for editor review.
  * Every observed `schema_version` is collected; surfacing an unknown one
    is the early-warning system for upstream breaking changes.
"""
from __future__ import annotations

import logging
from typing import Iterable

import requests
from django.conf import settings
from django.utils.text import slugify
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import AppDraft, BaseSource
from .models import Source

logger = logging.getLogger(__name__)


class MCPRegistrySchemaError(Exception):
    """Raised when a record's shape doesn't match the version we know."""


class MCPRegistrySource(BaseSource):
    """Ingest from the official MCP Registry."""

    source_type = Source.SourceType.MCP_REGISTRY
    KNOWN_SCHEMA_VERSIONS = {"1.0", "1.1"}
    PAGE_SIZE = 100

    def __init__(self, http: requests.Session | None = None) -> None:
        self.base_url: str = settings.MCP_REGISTRY_BASE_URL.rstrip("/")
        self.http = http or self._build_session()
        self.unparsed: list[dict] = []
        self.observed_schema_versions: set[str] = set()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _build_session() -> requests.Session:
        s = requests.Session()
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=3,
                backoff_factor=2,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET"]),
            )
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    def _server_url(self, external_id: str) -> str:
        return f"{self.base_url}/servers/{external_id}"

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------
    def iter_drafts(self) -> Iterable[AppDraft]:
        cursor: str | None = None
        while True:
            try:
                resp = self.http.get(
                    f"{self.base_url}/servers",
                    params={"cursor": cursor, "limit": self.PAGE_SIZE},
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.RequestException:
                logger.exception(
                    "mcp_registry_page_fetch_failed", extra={"cursor": cursor}
                )
                return

            payload = resp.json() or {}
            schema_version = str(payload.get("schema_version", "unknown"))
            self.observed_schema_versions.add(schema_version)

            for record in payload.get("servers", []):
                try:
                    yield self._normalize(record)
                except MCPRegistrySchemaError as exc:
                    self.unparsed.append(
                        {
                            "record": record,
                            "error": str(exc),
                            "schema_version": schema_version,
                        }
                    )
                    logger.warning(
                        "mcp_registry_unparsed",
                        extra={"id": record.get("id"), "error": str(exc)},
                    )
                    continue

            cursor = payload.get("next_cursor")
            if not cursor:
                break

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------
    def _normalize(self, record: dict) -> AppDraft:
        try:
            name = record["name"]
            external_id = record["id"]
        except KeyError as exc:
            raise MCPRegistrySchemaError(f"missing required field {exc}") from None

        transports = record.get("transports") or {}
        publisher = record.get("publisher") or {}
        install = record.get("install") or {}
        repo = record.get("repository") or {}

        capabilities = {
            "remote_available": (
                "yes" if transports.get("http") or transports.get("sse") else "unknown"
            ),
            "local_setup_required": (
                "yes" if transports.get("stdio") else "unknown"
            ),
            "open_source": "yes" if repo else "unknown",
        }

        transport_pick = next(
            (t for t in ("stdio", "sse", "http", "websocket") if transports.get(t)),
            None,
        )

        return AppDraft(
            name=name,
            slug_hint=slugify(name)[:200],
            short_description=(record.get("description") or "")[:280],
            long_description=record.get("description") or "",
            developer_name=publisher.get("name") or "",
            developer_url=publisher.get("url") or "",
            official_page_url=record.get("homepage") or "",
            install_url=install.get("url") or "",
            repo_url=repo.get("url") or "",
            platforms=["mcp"],
            listing_types=["mcp-server"],
            capabilities=capabilities,
            external_id=str(external_id),
            raw_payload=record,
            # The MCP Registry IS the platform directory in our taxonomy.
            official_directory_url=self._server_url(str(external_id)),
            platform_metadata={
                "protocol_version": record.get("protocol_version"),
                "transport": transport_pick,
                "repository_url": repo.get("url"),
                "install_command": install.get("command"),
                "required_env_vars": install.get("env", []) or [],
            },
        )
