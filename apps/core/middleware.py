"""Request-scoped middleware: correlation id + HTMX detection.

Architecture refs:
  * docs/architecture.md § 15.2 (structured logging carries request_id)
  * docs/architecture.md § 7 (HTMX patterns)
"""
from __future__ import annotations

import uuid
from typing import Callable

from django.http import HttpRequest, HttpResponse

REQUEST_ID_HEADER = "HTTP_X_REQUEST_ID"
REQUEST_ID_RESPONSE_HEADER = "X-Request-ID"


class RequestIDMiddleware:
    """Attach a stable UUID to every request and echo it back in headers.

    Downstream code (Celery tasks, structured logs, error reports) reads
    `request.request_id` so a user-visible UUID can be cross-referenced
    against backend logs.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.META.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.request_id = request_id  # type: ignore[attr-defined]
        response = self.get_response(request)
        response[REQUEST_ID_RESPONSE_HEADER] = request_id
        return response


class HtmxAwareMiddleware:
    """Expose `request.is_htmx` for views and templates.

    HTMX requests send `HX-Request: true`. Knowing this lets a single view
    return a full HTML page for cold loads and a partial for in-page swaps.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.is_htmx = request.headers.get("HX-Request") == "true"  # type: ignore[attr-defined]
        return self.get_response(request)
