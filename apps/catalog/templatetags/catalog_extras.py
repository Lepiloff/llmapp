"""Template helpers for catalog pages.

Includes a canonical-URL tag (used in `<link rel="canonical">`) that strips
query parameters so paginated or filter-suffixed URLs don't fragment SEO.
"""
from __future__ import annotations

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def canonical(context) -> str:
    """Return the absolute canonical URL for the current page."""
    request = context["request"]
    return request.build_absolute_uri(request.path)


@register.simple_tag(takes_context=True)
def query_replace(context, **kwargs) -> str:
    """Return the current querystring with one parameter replaced.

    Useful for pagination and sort links inside templates without hard-coding
    every other filter into the URL.
    """
    request = context["request"]
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()


@register.simple_tag
def trust_badges(app) -> list[dict]:
    """Return the three independent trust badges for a card."""
    from apps.catalog.models import App

    return [
        {
            "key": "platform",
            "label": "Listed on platform directory",
            "active": (
                app.platform_verification_status
                == App.PlatformVerificationStatus.OFFICIAL
            ),
        },
        {
            "key": "editorial",
            "label": "Reviewed by LLM App Market",
            "active": (
                app.editorial_review_status
                == App.EditorialReviewStatus.REVIEWED
            ),
        },
        {
            "key": "claim",
            "label": "Verified by developer",
            "active": (
                app.developer_claim_status
                == App.DeveloperClaimStatus.CLAIMED
            ),
        },
    ]
