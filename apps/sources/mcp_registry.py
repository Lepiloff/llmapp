"""MCP Registry ingest source.

Architecture refs:
  * docs/architecture.md § 9.2 (MCPRegistrySource)
  * docs/business.md § 13.5 (preview-status caveats)

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
from urllib.parse import quote

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


def _is_json_safe(value) -> bool:
    """Return True when ``value`` is something Django's JSONField can store as-is.

    The check is structural: dict / list / scalar primitives are all
    json.dumps-able. Anything exotic (custom objects, bytes, ...) is rejected
    so callers can fall back to ``repr(value)`` before persisting to the
    ``UnparsedRegistryRecord.payload`` JSONField, avoiding a serialization
    error that would mask the original parse failure.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_is_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(k, str) and _is_json_safe(v) for k, v in value.items()
        )
    return False


class MCPRegistrySource(BaseSource):
    """Ingest from the official MCP Registry."""

    source_type = Source.SourceType.MCP_REGISTRY
    KNOWN_SCHEMA_VERSIONS = {"1.0", "1.1"}
    PAGE_SIZE = 100

    def __init__(
        self,
        http: requests.Session | None = None,
        *,
        start_cursor: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self.base_url: str = settings.MCP_REGISTRY_BASE_URL.rstrip("/")
        self.http = http or self._build_session()
        self.start_cursor = start_cursor or None
        self.request_timeout = (
            request_timeout
            if request_timeout is not None
            else float(getattr(settings, "MCP_REGISTRY_TIMEOUT_SECONDS", 90.0) or 90.0)
        )
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
        return f"{self.base_url}/servers/{quote(external_id, safe='')}"

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------
    def iter_drafts(self) -> Iterable[AppDraft]:
        cursor: str | None = self.start_cursor
        while True:
            try:
                resp = self.http.get(
                    f"{self.base_url}/servers",
                    params={"cursor": cursor, "limit": self.PAGE_SIZE},
                    timeout=self.request_timeout,
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.exception(
                    "mcp_registry_page_fetch_failed", extra={"cursor": cursor}
                )
                _report_to_sentry(exc, base_url=self.base_url, cursor=cursor)
                return

            # The Registry is preview status (business.md § 13.5): every
            # external shape we touch is treated as untrusted.
            try:
                payload = resp.json()
            except ValueError:
                # JSONDecodeError is a subclass of ValueError on stdlib + requests.
                logger.exception(
                    "mcp_registry_invalid_json", extra={"cursor": cursor}
                )
                return

            if not isinstance(payload, dict):
                logger.warning(
                    "mcp_registry_payload_not_dict",
                    extra={
                        "cursor": cursor,
                        "received_type": type(payload).__name__,
                    },
                )
                return

            schema_version = str(payload.get("schema_version") or "unknown")
            self.observed_schema_versions.add(schema_version)

            servers = payload.get("servers")
            if servers is None:
                # Missing key — treat as empty page; honour next_cursor below.
                servers = []
            if not isinstance(servers, list):
                logger.warning(
                    "mcp_registry_servers_not_list",
                    extra={
                        "cursor": cursor,
                        "schema_version": schema_version,
                        "received_type": type(servers).__name__,
                    },
                )
                return

            for record in servers:
                try:
                    yield self._normalize(record)
                except MCPRegistrySchemaError as exc:
                    self.unparsed.append(
                        {
                            "record": record if _is_json_safe(record) else repr(record),
                            "error": str(exc),
                            "schema_version": schema_version,
                        }
                    )
                    logger.warning(
                        "mcp_registry_unparsed",
                        extra={
                            "record_id": (
                                record.get("id") if isinstance(record, dict) else None
                            ),
                            "error": str(exc),
                        },
                    )
                    continue

            cursor = payload.get("next_cursor")
            if cursor is None and isinstance(payload.get("metadata"), dict):
                cursor = payload["metadata"].get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------
    def _normalize(self, record) -> AppDraft:
        if not isinstance(record, dict):
            raise MCPRegistrySchemaError(
                f"record is not a dict (got {type(record).__name__})"
            )
        original_record = record
        meta = record.get("_meta") if isinstance(record.get("_meta"), dict) else {}
        if isinstance(record.get("server"), dict):
            record = record["server"]
        schema_url = str(record.get("$schema") or "")
        if schema_url:
            self.observed_schema_versions.add(schema_url)
        try:
            name = record["name"]
        except KeyError as exc:
            raise MCPRegistrySchemaError(f"missing required field {exc}") from None
        external_id = str(record.get("id") or name)
        display_name = str(record.get("title") or name)

        transports = record.get("transports") or {}
        publisher = record.get("publisher") or {}
        install = record.get("install") or {}
        repo = record.get("repository") or {}
        remotes = record.get("remotes") if isinstance(record.get("remotes"), list) else []
        packages = record.get("packages") if isinstance(record.get("packages"), list) else []
        official_meta = meta.get("io.modelcontextprotocol.registry/official")
        if not isinstance(official_meta, dict):
            official_meta = {}

        has_remote = bool(
            transports.get("http")
            or transports.get("sse")
            or any(isinstance(remote, dict) and remote.get("url") for remote in remotes)
        )
        has_local = bool(transports.get("stdio") or packages)

        capabilities = {
            "remote_available": "yes" if has_remote else "unknown",
            "local_setup_required": "yes" if has_local else "unknown",
            "open_source": "yes" if repo else "unknown",
        }

        transport_pick = next(
            (t for t in ("stdio", "sse", "http", "websocket") if transports.get(t)),
            None,
        )
        if transport_pick is None:
            transport_pick = next(
                (
                    str(remote.get("type"))
                    for remote in remotes
                    if isinstance(remote, dict) and remote.get("type")
                ),
                None,
            )

        remote_url = next(
            (
                str(remote.get("url"))
                for remote in remotes
                if isinstance(remote, dict) and remote.get("url")
            ),
            "",
        )
        package_url = next(
            (
                str(package.get("url"))
                for package in packages
                if isinstance(package, dict) and package.get("url")
            ),
            "",
        )

        return AppDraft(
            name=display_name,
            slug_hint=slugify(display_name or name)[:200],
            short_description=(record.get("description") or "")[:280],
            long_description=record.get("description") or "",
            developer_name=publisher.get("name") or "",
            developer_url=publisher.get("url") or "",
            official_page_url=record.get("homepage") or repo.get("url") or remote_url,
            install_url=install.get("url") or package_url,
            repo_url=repo.get("url") or "",
            platforms=["mcp"],
            listing_types=["mcp-server"],
            capabilities=capabilities,
            external_id=external_id,
            raw_payload=original_record,
            # The MCP Registry IS the platform directory in our taxonomy.
            official_directory_url=self._server_url(external_id),
            platform_metadata={
                "protocol_version": record.get("protocol_version"),
                "version": record.get("version"),
                "transport": transport_pick,
                "repository_url": repo.get("url"),
                "install_command": install.get("command"),
                "required_env_vars": install.get("env", []) or [],
                "remotes": remotes,
                "packages": packages,
                "is_latest": official_meta.get("isLatest"),
                "status": official_meta.get("status"),
                "published_at": official_meta.get("publishedAt"),
                "updated_at": official_meta.get("updatedAt"),
            },
        )


def _report_to_sentry(
    exc: requests.RequestException,
    *,
    base_url: str,
    cursor: str | None,
) -> None:
    """Surface MCP Registry outages in Sentry without spamming the issue list.

    A persistent 404 on the registry endpoint (e.g. upstream moved the URL
    or shut the API down) was previously invisible — the task counter
    returned zero, beat kept firing, no one noticed. We now capture each
    fetch failure into Sentry with a stable ``fingerprint`` so the issue
    coalesces into a single group across daily reruns, and tag the HTTP
    status when known.

    Soft dependency: ``sentry_sdk`` is only used when configured (DSN set).
    The import is local because the module imports cleanly without it.
    """
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover - sentry-sdk is in pyproject
        return

    status_code = None
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)

    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = ["mcp-registry-unreachable", str(status_code or "transport")]
        scope.set_tag("component", "mcp_registry_ingest")
        scope.set_tag("http_status", str(status_code) if status_code else "unknown")
        scope.set_context(
            "mcp_registry",
            {"base_url": base_url, "cursor": cursor, "error_class": type(exc).__name__},
        )
        sentry_sdk.capture_message(
            f"MCP Registry fetch failed ({status_code or 'transport error'})",
            level="warning",
        )
