"""Structured data generation for Schema.org JSON-LD.

Architecture refs:
  * docs/architecture.md § 11 (structured data)
"""
from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.catalog.models import App
from apps.editorial.models import Post


def generate_app_json_ld(app: App) -> dict[str, Any]:
    """Generate JSON-LD structured data for an app."""
    data = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": app.name,
        "description": app.short_description,
        "url": f"{settings.SITE_BASE_URL}{app.get_absolute_url()}",
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web",
        "offers": {
            "@type": "Offer",
            "price": "0" if app.pricing_model == "free" else "varies",
            "priceCurrency": "USD",
        },
    }

    # Add developer info if available
    if app.developer_name:
        data["author"] = {
            "@type": "Organization",
            "name": app.developer_name,
        }
        if app.developer_url:
            data["author"]["url"] = app.developer_url

    # Add install URL if available
    if app.install_url:
        data["downloadUrl"] = app.install_url

    # Add logo if available
    if app.logo:
        data["image"] = f"{settings.SITE_BASE_URL}{app.logo.url}"

    # Add categories
    if app.categories.exists():
        categories = list(app.categories.values_list('name', flat=True))
        if len(categories) == 1:
            data["category"] = categories[0]
        else:
            data["category"] = categories

    # Add platforms as compatible applications
    if app.platforms.exists():
        platform_names = list(app.platforms.values_list('name', flat=True))
        data["requirements"] = f"Compatible with: {', '.join(platform_names)}"

    # Add aggregated rating if quality score is high
    if app.quality_score >= 70:
        data["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": app.quality_score / 20,  # Convert 0-100 to 0-5 scale
            "bestRating": 5,
            "worstRating": 1,
        }

    return data


def generate_article_json_ld(post: Post) -> dict[str, Any]:
    """Generate JSON-LD structured data for a blog post."""
    data = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": post.title,
        "description": post.excerpt,
        "url": f"{settings.SITE_BASE_URL}{post.get_absolute_url()}",
        "datePublished": post.published_at.isoformat() if post.published_at else None,
        "dateModified": post.updated_at.isoformat(),
        "author": {
            "@type": "Person",
            "name": post.author.get_full_name() or post.author.username,
        },
        "publisher": {
            "@type": "Organization",
            "name": settings.SITE_NAME,
            "url": settings.SITE_BASE_URL,
        },
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": f"{settings.SITE_BASE_URL}{post.get_absolute_url()}",
        },
    }

    # Add cover image if available
    if post.cover_image:
        data["image"] = {
            "@type": "ImageObject",
            "url": f"{settings.SITE_BASE_URL}{post.cover_image.url}",
            "caption": post.cover_alt_text or post.title,
        }

    # Add article type based on post type
    if post.post_type == "guide":
        data["@type"] = "HowTo"
    elif post.post_type == "news":
        data["@type"] = "NewsArticle"

    # Add reading time
    if post.reading_time_minutes:
        data["timeRequired"] = f"PT{post.reading_time_minutes}M"

    return data


def generate_organization_json_ld() -> dict[str, Any]:
    """Generate JSON-LD structured data for the site organization."""
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": settings.SITE_NAME,
        "url": settings.SITE_BASE_URL,
        "logo": f"{settings.SITE_BASE_URL}/static/img/logo.png",
        "description": "Discover apps, connectors and agents for ChatGPT, Claude, Gemini and beyond.",
        "sameAs": [
            # Add social media URLs here when available
        ],
    }


def generate_website_json_ld() -> dict[str, Any]:
    """Generate JSON-LD structured data for the website."""
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": settings.SITE_NAME,
        "url": settings.SITE_BASE_URL,
        "description": "Discover apps, connectors and agents for ChatGPT, Claude, Gemini and beyond.",
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": f"{settings.SITE_BASE_URL}/search/?q={{search_term_string}}",
            },
            "query-input": "required name=search_term_string",
        },
    }


def generate_breadcrumb_json_ld(breadcrumbs: list) -> dict[str, Any]:
    """Generate JSON-LD structured data for breadcrumb navigation.

    Args:
        breadcrumbs: List of tuples (name, url)
    """
    items = []
    for i, (name, url) in enumerate(breadcrumbs):
        items.append({
            "@type": "ListItem",
            "position": i + 1,
            "name": name,
            "item": f"{settings.SITE_BASE_URL}{url}" if url else None,
        })

    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": items,
    }
