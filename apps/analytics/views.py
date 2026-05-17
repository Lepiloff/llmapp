"""Analytics views for tracking clicks and page views."""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit

from apps.catalog.models import App
from .models import ClickEvent, PageView
from .utils import get_client_ip, get_session_key

logger = logging.getLogger(__name__)


# Per-IP rate cap on outbound redirects. The endpoint writes one ClickEvent
# per request and feeds the trending score; without a cap an attacker can
# inflate trending rankings or simply fill the DB. 60/min is generous for
# real users (you'd need to click 60 links/minute to hit it) and tight
# enough that scripted abuse is throttled.
_OUTBOUND_RATE = "60/m"


def _xff_aware_ip_key(_group, request: HttpRequest) -> str:
    """Custom django-ratelimit key that respects X-Forwarded-For.

    The default ``key='ip'`` reads ``REMOTE_ADDR``, which behind nginx /
    ALB is the proxy's address — all real clients would share one
    bucket and a single noisy user would 403 outbound clicks for
    everyone. We mirror ``apps.analytics.utils.get_client_ip`` which
    picks the first hop from ``X-Forwarded-For``, so the same client
    IP policy applies across click tracking and throttling.
    """
    return get_client_ip(request) or "unknown"


@never_cache
@require_http_methods(["GET"])
@ratelimit(key=_xff_aware_ip_key, rate=_OUTBOUND_RATE, method="GET", block=True)
def outbound_redirect(request: HttpRequest, slug: str) -> HttpResponse:
    """Tracked outbound redirect for app links.

    URL format: /go/<app_slug>/?url=<target_url>&type=<link_type>&src=<source_page>&pos=<position>

    This allows us to track which links users click while providing
    a seamless user experience.
    """
    app = get_object_or_404(App.published.all(), slug=slug)

    # Get target URL and validate it
    target_url = request.GET.get('url', '').strip()
    link_type = request.GET.get('type', 'official')
    source_page = request.GET.get('src', '')
    source_position = request.GET.get('pos')

    if not target_url:
        raise Http404("Target URL is required")

    # Validate URL format
    validator = URLValidator()
    try:
        validator(target_url)
    except ValidationError:
        raise Http404("Invalid target URL")

    # Security check: only allow redirects to expected domains
    parsed_url = urlparse(target_url)
    allowed_schemes = {'http', 'https'}
    if parsed_url.scheme not in allowed_schemes:
        raise Http404("Invalid URL scheme")

    # Validate link type against app's actual links
    if not _is_valid_link_for_app(app, target_url, link_type):
        logger.warning(f"Invalid link tracking attempt: {target_url} for {app.slug}")
        raise Http404("Invalid link for this app")

    # Convert position to integer if provided
    position = None
    if source_position:
        try:
            position = int(source_position)
        except (ValueError, TypeError):
            pass

    # Track the click event
    try:
        ClickEvent.objects.create(
            app=app,
            link_type=link_type,
            source_page=source_page[:100],  # Truncate to field limit
            source_position=position,
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:1000],
            ip_address=get_client_ip(request),
            referer=request.META.get('HTTP_REFERER', '')[:500],
            session_key=get_session_key(request),
        )
    except Exception as e:
        # Don't fail the redirect if tracking fails
        logger.error(f"Failed to track click for {app.slug}: {e}")

    # Redirect to target URL
    return HttpResponseRedirect(target_url)


def track_page_view(request: HttpRequest, page_type: str, page_identifier: str = '') -> None:
    """Track a page view for analytics.

    Call this from views to track page visits.
    """
    try:
        PageView.objects.create(
            page_type=page_type,
            page_identifier=page_identifier[:200],  # Truncate to field limit
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:1000],
            ip_address=get_client_ip(request),
            referer=request.META.get('HTTP_REFERER', '')[:500],
            session_key=get_session_key(request),
        )
    except Exception as e:
        # Don't fail the request if tracking fails
        logger.error(f"Failed to track page view {page_type}:{page_identifier}: {e}")


def _is_valid_link_for_app(app: App, target_url: str, link_type: str) -> bool:
    """Validate that the target URL is actually associated with this app.

    Security measure to prevent abuse of the redirect tracking.
    """
    if link_type == 'official' and app.official_page_url:
        return target_url == app.official_page_url

    if link_type == 'install' and app.install_url:
        return target_url == app.install_url

    if link_type == 'repo' and app.repo_url:
        return target_url == app.repo_url

    if link_type == 'platform':
        # Check platform directory URLs
        platform_urls = app.platform_links.values_list(
            'official_directory_url', 'install_url', flat=False
        )
        for directory_url, install_url in platform_urls:
            if target_url in (directory_url, install_url):
                return True

    return False