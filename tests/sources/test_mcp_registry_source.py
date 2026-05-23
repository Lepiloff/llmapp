"""Regression tests for MCPRegistrySource hardening (Phase 0 follow-up).

The MCP Registry is in **preview** status (docs/business.md § 13.5): the
schema is explicitly subject to breaking changes. ``iter_drafts`` must
therefore survive every untrusted shape the upstream API can produce —
malformed JSON, payload that isn't an object, ``servers`` that isn't a
list, records that aren't dicts, records missing required fields — without
aborting the batch or raising into Celery.

These tests inject a stubbed ``requests.Session`` into ``MCPRegistrySource``
so we can replay arbitrary upstream responses without network access.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from apps.sources.base import AppDraft
from apps.sources.mcp_registry import MCPRegistrySchemaError, MCPRegistrySource


def _response(payload=None, *, raise_on_status: Exception | None = None,
              json_exception: Exception | None = None) -> MagicMock:
    """Build a Response-like mock.

    * ``payload`` — what ``resp.json()`` returns when called.
    * ``json_exception`` — if set, ``resp.json()`` raises this instead.
    * ``raise_on_status`` — if set, ``resp.raise_for_status()`` raises this.
    """
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 500 if raise_on_status else 200

    if raise_on_status is not None:
        resp.raise_for_status.side_effect = raise_on_status
    else:
        resp.raise_for_status.return_value = None

    if json_exception is not None:
        resp.json.side_effect = json_exception
    else:
        resp.json.return_value = payload
    return resp


def _source_with(*responses: MagicMock) -> MCPRegistrySource:
    """Return an MCPRegistrySource whose HTTP session yields the given responses."""
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = list(responses)
    return MCPRegistrySource(http=session)


# ---------------------------------------------------------------------------
# Malformed transport layer
# ---------------------------------------------------------------------------
def test_invalid_json_returns_empty_without_raise() -> None:
    """``resp.json()`` raising must not surface as a Celery failure."""
    source = _source_with(
        _response(json_exception=json.JSONDecodeError("expecting value", "", 0))
    )

    drafts = list(source.iter_drafts())

    assert drafts == []
    assert source.unparsed == []
    # We never reached the schema_version assignment.
    assert source.observed_schema_versions == set()


def test_http_error_returns_empty_without_raise() -> None:
    """``resp.raise_for_status()`` raising must not surface as a Celery failure."""
    source = _source_with(
        _response(raise_on_status=requests.HTTPError("synthetic 500"))
    )

    drafts = list(source.iter_drafts())

    assert drafts == []


# ---------------------------------------------------------------------------
# Malformed payload shapes
# ---------------------------------------------------------------------------
def test_payload_is_a_list_returns_empty() -> None:
    """A registry response of ``[1, 2, 3]`` is structurally invalid."""
    source = _source_with(_response(payload=[1, 2, 3]))

    drafts = list(source.iter_drafts())

    assert drafts == []
    # We never reached schema_version because payload wasn't a dict.
    assert source.observed_schema_versions == set()


def test_payload_is_a_string_returns_empty() -> None:
    source = _source_with(_response(payload="just a string"))

    drafts = list(source.iter_drafts())

    assert drafts == []


def test_servers_is_not_a_list_returns_empty_but_records_schema_version() -> None:
    """``servers: "oops"`` is invalid; schema_version is still observable."""
    source = _source_with(
        _response(payload={"servers": "oops", "schema_version": "1.0"})
    )

    drafts = list(source.iter_drafts())

    assert drafts == []
    assert source.observed_schema_versions == {"1.0"}


def test_servers_is_none_returns_empty_gracefully() -> None:
    """``servers: null`` is treated as an empty page, not a crash."""
    source = _source_with(
        _response(payload={"servers": None, "schema_version": "1.0"})
    )

    drafts = list(source.iter_drafts())

    assert drafts == []
    assert source.observed_schema_versions == {"1.0"}


def test_servers_missing_returns_empty_gracefully() -> None:
    source = _source_with(_response(payload={"schema_version": "1.0"}))

    drafts = list(source.iter_drafts())

    assert drafts == []
    assert source.observed_schema_versions == {"1.0"}


# ---------------------------------------------------------------------------
# Malformed record shapes (per-record isolation)
# ---------------------------------------------------------------------------
def test_non_dict_records_are_routed_to_unparsed() -> None:
    """Records that aren't dicts are unparsed entries — never crashes."""
    source = _source_with(
        _response(payload={
            "servers": ["not a dict", 42, None, ["nested-list"]],
            "schema_version": "1.0",
        })
    )

    drafts = list(source.iter_drafts())

    assert drafts == []
    assert len(source.unparsed) == 4
    for entry in source.unparsed:
        assert "record is not a dict" in entry["error"]
        assert entry["schema_version"] == "1.0"


def test_record_missing_required_field_is_unparsed() -> None:
    source = _source_with(
        _response(payload={
            "servers": [{"description": "no id or name"}],
            "schema_version": "1.0",
        })
    )

    drafts = list(source.iter_drafts())

    assert drafts == []
    assert len(source.unparsed) == 1
    assert "missing required field" in source.unparsed[0]["error"]


def test_partial_batch_continues_after_bad_records() -> None:
    """One bad record between two good ones must not block the others."""
    source = _source_with(
        _response(payload={
            "servers": [
                {"id": "good-1", "name": "Good One", "description": "x"},
                "this string should be unparsed",
                {"id": "good-2", "name": "Good Two"},
            ],
            "schema_version": "1.0",
        })
    )

    drafts = list(source.iter_drafts())

    assert len(drafts) == 2
    assert {d.name for d in drafts} == {"Good One", "Good Two"}
    assert len(source.unparsed) == 1
    assert "record is not a dict" in source.unparsed[0]["error"]


# ---------------------------------------------------------------------------
# Happy path — pagination + valid normalization
# ---------------------------------------------------------------------------
def test_happy_path_single_page_yields_valid_draft() -> None:
    source = _source_with(
        _response(payload={
            "servers": [{
                "id": "srv-001",
                "name": "ExampleMCP",
                "description": "Demo server",
                "transports": {"stdio": True},
                "repository": {"url": "https://github.com/x/y"},
                "publisher": {"name": "Acme", "url": "https://acme.example"},
            }],
            "schema_version": "1.0",
        })
    )

    drafts = list(source.iter_drafts())

    assert len(drafts) == 1
    draft = drafts[0]
    assert isinstance(draft, AppDraft)
    assert draft.name == "ExampleMCP"
    assert draft.external_id == "srv-001"
    assert draft.platforms == ["mcp"]
    assert draft.listing_types == ["mcp-server"]
    assert draft.capabilities["local_setup_required"] == "yes"
    assert draft.capabilities["open_source"] == "yes"


def test_pagination_stops_when_next_cursor_is_not_string() -> None:
    """Defensive: a malformed ``next_cursor`` (None, number, list) ends pagination."""
    source = _source_with(
        _response(payload={
            "servers": [{"id": "p1", "name": "P1"}],
            "next_cursor": {"weird": "object"},  # not a string
            "schema_version": "1.0",
        })
    )

    drafts = list(source.iter_drafts())

    # Page 1 yielded; pagination did not chase a malformed cursor (no second call).
    assert len(drafts) == 1


def test_pagination_follows_string_next_cursor() -> None:
    """Two-page happy path: page 2 fetched when next_cursor is a string."""
    source = _source_with(
        _response(payload={
            "servers": [{"id": "p1", "name": "P1"}],
            "next_cursor": "page-2",
            "schema_version": "1.0",
        }),
        _response(payload={
            "servers": [{"id": "p2", "name": "P2"}],
            "schema_version": "1.0",
        }),
    )

    drafts = list(source.iter_drafts())

    assert {d.external_id for d in drafts} == {"p1", "p2"}
