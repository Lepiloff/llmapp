"""Regression: HEAD-405/501 must fall back to GET, not accumulate failures.

Many SaaS endpoints (and some GitHub Pages sites) return 405 on HEAD
even when the URL is perfectly live. Without a fallback, those URLs
would tick consecutive_failures up every link-check pass and trip
auto-deprecation after 7 cycles even though they were never down.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apps.sources.tasks import _probe_url


@pytest.mark.parametrize("happy_code", [200, 204, 301, 302, 308, 401, 404])
def test_head_status_is_returned_when_head_works(happy_code: int) -> None:
    with patch("apps.sources.tasks.requests.head") as fake_head, \
         patch("apps.sources.tasks.requests.get") as fake_get:
        fake_head.return_value = MagicMock(status_code=happy_code)
        assert _probe_url("https://example.com") == happy_code
        fake_get.assert_not_called()


@pytest.mark.parametrize("reject_code", [403, 405, 501])
def test_head_reject_falls_back_to_get_with_range(reject_code: int) -> None:
    with patch("apps.sources.tasks.requests.head") as fake_head, \
         patch("apps.sources.tasks.requests.get") as fake_get:
        fake_head.return_value = MagicMock(status_code=reject_code)
        fake_get_resp = MagicMock(status_code=200)
        fake_get.return_value = fake_get_resp

        assert _probe_url("https://example.com") == 200

        fake_get.assert_called_once()
        # Range header is sent so we don't pull the whole page.
        kwargs = fake_get.call_args.kwargs
        assert kwargs["headers"]["Range"].startswith("bytes=0-")
        assert kwargs["stream"] is True
        fake_get_resp.close.assert_called_once()


def test_get_fallback_propagates_status() -> None:
    """If HEAD is 405 and GET also fails, return the GET status."""
    with patch("apps.sources.tasks.requests.head") as fake_head, \
         patch("apps.sources.tasks.requests.get") as fake_get:
        fake_head.return_value = MagicMock(status_code=405)
        fake_get.return_value = MagicMock(status_code=503)

        assert _probe_url("https://example.com") == 503
