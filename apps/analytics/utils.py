"""Analytics utility functions."""
from __future__ import annotations

from django.http import HttpRequest


def get_client_ip(request: HttpRequest) -> str:
    """Extract client IP address from request."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def get_session_key(request: HttpRequest) -> str:
    """Get session key for tracking, creating one if needed."""
    if not hasattr(request, 'session'):
        return ''

    if not request.session.session_key:
        request.session.create()

    return request.session.session_key or ''