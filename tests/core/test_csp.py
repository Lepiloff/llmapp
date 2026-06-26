"""CSP middleware regressions."""
from __future__ import annotations

import re

from django.http import HttpResponse
from django.test import RequestFactory

from apps.core.csp import (
    GOOGLE_FONTS_CSS_ORIGIN,
    GOOGLE_FONTS_FILES_ORIGIN,
    TAILWIND_CDN_ORIGIN,
    TURNSTILE_SCRIPT_ORIGIN,
    UNPKG_CDN_ORIGIN,
    CSPMiddleware,
)


def _run(request, response_factory=lambda req: HttpResponse("ok")):
    mw = CSPMiddleware(response_factory)
    return mw(request)


def test_response_carries_csp_header(rf: RequestFactory) -> None:
    response = _run(rf.get("/"))
    assert "Content-Security-Policy" in response
    policy = response["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert TURNSTILE_SCRIPT_ORIGIN in policy
    assert "frame-ancestors 'none'" in policy


def test_request_gets_nonce(rf: RequestFactory) -> None:
    captured = {}

    def _capture(req):
        captured["nonce"] = req.csp_nonce
        return HttpResponse("ok")

    _run(rf.get("/"), response_factory=_capture)
    assert captured["nonce"]
    # token_urlsafe(16) → 22-char base64-safe string
    assert re.match(r"^[A-Za-z0-9_-]{16,}$", captured["nonce"])


def test_nonce_is_per_request(rf: RequestFactory) -> None:
    nonces: list[str] = []

    def _capture(req):
        nonces.append(req.csp_nonce)
        return HttpResponse("ok")

    _run(rf.get("/"), response_factory=_capture)
    _run(rf.get("/"), response_factory=_capture)
    assert nonces[0] != nonces[1]


def test_existing_csp_header_not_overwritten(rf: RequestFactory) -> None:
    def _custom_policy(req):
        resp = HttpResponse("ok")
        resp["Content-Security-Policy"] = "default-src 'none'"
        return resp

    response = _run(rf.get("/"), response_factory=_custom_policy)
    assert response["Content-Security-Policy"] == "default-src 'none'"


def test_nonce_appears_in_script_src(rf: RequestFactory) -> None:
    captured = {}

    def _capture(req):
        captured["nonce"] = req.csp_nonce
        return HttpResponse("ok")

    response = _run(rf.get("/"), response_factory=_capture)
    assert f"'nonce-{captured['nonce']}'" in response["Content-Security-Policy"]


def test_policy_allows_actual_frontend_origins(rf: RequestFactory) -> None:
    """base.html ships Tailwind / htmx / Alpine via CDNs and Google
    Fonts. CSP must allow each, otherwise the prod homepage breaks.
    """
    response = _run(rf.get("/"))
    policy = response["Content-Security-Policy"]
    # scripts
    assert TAILWIND_CDN_ORIGIN in policy
    assert UNPKG_CDN_ORIGIN in policy
    # fonts CSS + .woff2 files
    assert GOOGLE_FONTS_CSS_ORIGIN in policy
    assert GOOGLE_FONTS_FILES_ORIGIN in policy
    # img-src tolerates the https-loaded logos third-party developers link.
    assert "img-src 'self' data: https:" in policy
