"""Content-Security-Policy middleware.

Defence-in-depth for a catalog that lists user-submitted URLs and
embeds a third-party CAPTCHA widget plus a set of CDN-hosted
frontend assets (Tailwind, htmx, Alpine, Google Fonts). The policy:

* default-src 'self' — every resource type defaults to same-origin.
* script-src adds the Cloudflare Turnstile origin, the unpkg CDN
  (htmx + Alpine), the Tailwind play-CDN, and a per-request nonce
  so template-rendered inline scripts (Tailwind config block,
  JSON-LD structured data) keep working.
* style-src includes Google Fonts CSS host + 'unsafe-inline' (Django
  admin sprays inline styles; vendoring admin CSS is a separate
  cleanup pass).
* font-src adds the Google Fonts CDN (gstatic.com).
* frame-src lists challenges.cloudflare.com so the Turnstile iframe
  loads; connect-src adds the same origin for the verify XHR.
* img-src 'self' data: https: — third-party logos referenced from
  cards (also previewed by social-card meta) need https sources.
* frame-ancestors 'none' + object-src 'none' + base-uri 'self'
  block clickjacking, Flash-style legacy embeds, and base-tag tricks.

Operators tightening this further should vendor htmx / Alpine /
Tailwind under /static/ and drop the corresponding origins from the
allowlist. Google Fonts can be eliminated by self-hosting the .woff2
files; tracked as future work.

Each request gets ``request.csp_nonce`` so templates can render
``<script nonce="{{ request.csp_nonce }}">…</script>`` when an
inline ``<script>`` is unavoidable.
"""
from __future__ import annotations

import secrets
from typing import Callable

from django.http import HttpRequest, HttpResponse


TURNSTILE_SCRIPT_ORIGIN = "https://challenges.cloudflare.com"
TAILWIND_CDN_ORIGIN = "https://cdn.tailwindcss.com"
UNPKG_CDN_ORIGIN = "https://unpkg.com"
GOOGLE_FONTS_CSS_ORIGIN = "https://fonts.googleapis.com"
GOOGLE_FONTS_FILES_ORIGIN = "https://fonts.gstatic.com"


def _build_policy(nonce: str) -> str:
    """Return the canonical CSP header value, parametrised by nonce."""
    script_sources = (
        f"'self' 'nonce-{nonce}' "
        f"{TURNSTILE_SCRIPT_ORIGIN} {TAILWIND_CDN_ORIGIN} {UNPKG_CDN_ORIGIN}"
    )
    style_sources = (
        f"'self' 'unsafe-inline' {GOOGLE_FONTS_CSS_ORIGIN}"
    )
    directives = [
        ("default-src", "'self'"),
        ("script-src", script_sources),
        ("style-src", style_sources),
        ("img-src", "'self' data: https:"),
        ("font-src", f"'self' data: {GOOGLE_FONTS_FILES_ORIGIN}"),
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
