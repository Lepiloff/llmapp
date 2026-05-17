"""Content-Security-Policy middleware.

Defence-in-depth for a catalog that lists user-submitted URLs and
embeds a third-party CAPTCHA widget. The policy:

* default-src 'self' — every resource type defaults to same-origin.
* script-src adds the Cloudflare Turnstile origin (where the widget
  loads its JS) plus a per-request nonce so we can keep inline
  ``<script>`` tags (template-rendered, never user-data) working.
* frame-src lists challenges.cloudflare.com so the Turnstile iframe
  loads.
* connect-src adds the same origin — Turnstile posts back to verify.
* img-src 'self' data: — covers placeholder PNGs and inline SVG data
  URIs.
* style-src adds 'unsafe-inline' for now — Django admin sprays inline
  styles. Tightening this would require an admin-specific bypass and
  is deferred.
* report-uri intentionally omitted; turn on once we have a sink.

Each request gets ``request.csp_nonce`` so templates can render
``<script nonce="{{ request.csp_nonce }}">…</script>`` when an
inline ``<script>`` is unavoidable. The Turnstile widget receives
the same nonce so its inline form fragment is allowed.
"""
from __future__ import annotations

import secrets
from typing import Callable

from django.http import HttpRequest, HttpResponse


TURNSTILE_SCRIPT_ORIGIN = "https://challenges.cloudflare.com"


def _build_policy(nonce: str) -> str:
    """Return the canonical CSP header value, parametrised by nonce."""
    directives = [
        ("default-src", "'self'"),
        (
            "script-src",
            f"'self' 'nonce-{nonce}' {TURNSTILE_SCRIPT_ORIGIN}",
        ),
        ("style-src", "'self' 'unsafe-inline'"),
        ("img-src", "'self' data:"),
        ("font-src", "'self' data:"),
        ("connect-src", f"'self' {TURNSTILE_SCRIPT_ORIGIN}"),
        ("frame-src", TURNSTILE_SCRIPT_ORIGIN),
        ("frame-ancestors", "'none'"),
        ("base-uri", "'self'"),
        ("form-action", "'self'"),
        ("object-src", "'none'"),
    ]
    return "; ".join(f"{name} {value}" for name, value in directives)


class CSPMiddleware:
    """Attach a per-request CSP nonce and emit the header on every response.

    Stays a single class (no factory / settings registry) because the
    policy is fixed at the project level; per-view exceptions are
    handled via ``request.csp_nonce`` in templates.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Cryptographically random nonce per request — base64 chars are
        # CSP-safe.
        nonce = secrets.token_urlsafe(16)
        request.csp_nonce = nonce  # type: ignore[attr-defined]

        response = self.get_response(request)
        # Skip the header on responses that already carry one
        # (e.g. an upstream proxy added a stricter policy).
        if "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = _build_policy(nonce)
        return response
