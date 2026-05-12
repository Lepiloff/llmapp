"""SEO context processors."""
from __future__ import annotations

from typing import Dict, Any
from django.http import HttpRequest
from django.conf import settings

from .structured_data import generate_organization_json_ld, generate_website_json_ld


def seo_meta(request: HttpRequest) -> Dict[str, Any]:
    """Add SEO metadata to template context."""
    return {
        'seo_meta': {
            'site_name': settings.SITE_NAME,
            'site_base_url': settings.SITE_BASE_URL,
            'default_meta_title': settings.SITE_NAME,
            'default_meta_description': getattr(settings, 'SITE_TAGLINE', ''),
            'organization_json_ld': generate_organization_json_ld(),
            'website_json_ld': generate_website_json_ld(),
        }
    }