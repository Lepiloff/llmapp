"""Sitemaps for SEO.

Architecture refs:
  * docs/architecture.md § 14.2 (sitemap generation)
"""
from __future__ import annotations

from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from apps.catalog.models import App, Category, Platform
from apps.editorial.models import Post, Collection, Comparison


class AppsSitemap(Sitemap):
    """Sitemap for published app detail pages."""

    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return App.published.all().order_by('-quality_score')

    def lastmod(self, obj):
        return obj.updated_at

    def location(self, obj):
        return obj.get_absolute_url()


class CategoriesSitemap(Sitemap):
    """Sitemap for category pages."""

    changefreq = "daily"
    priority = 0.7

    def items(self):
        # Only include categories with published apps
        return Category.objects.filter(
            apps__status='published'
        ).distinct().order_by('sort_order', 'name')

    def location(self, obj):
        return obj.get_absolute_url()


class PlatformsSitemap(Sitemap):
    """Sitemap for platform pages."""

    changefreq = "daily"
    priority = 0.7

    def items(self):
        # Only include platforms with published apps
        return Platform.objects.filter(
            apps__status='published'
        ).distinct().order_by('sort_order', 'name')

    def location(self, obj):
        return obj.get_absolute_url()


class PostsSitemap(Sitemap):
    """Sitemap for blog posts."""

    changefreq = "monthly"
    priority = 0.6

    def items(self):
        return Post.objects.filter(
            status=Post.Status.PUBLISHED
        ).order_by('-published_at')

    def lastmod(self, obj):
        return obj.updated_at

    def location(self, obj):
        return obj.get_absolute_url()


class CollectionsSitemap(Sitemap):
    """Sitemap for app collections."""

    changefreq = "monthly"
    priority = 0.6

    def items(self):
        return Collection.objects.filter(
            is_published=True
        ).order_by('-created_at')

    def lastmod(self, obj):
        return obj.updated_at

    def location(self, obj):
        return obj.get_absolute_url()


class ComparisonsSitemap(Sitemap):
    """Sitemap for app comparisons."""

    changefreq = "monthly"
    priority = 0.6

    def items(self):
        return Comparison.objects.filter(
            is_published=True
        ).order_by('-created_at')

    def lastmod(self, obj):
        return obj.updated_at

    def location(self, obj):
        return obj.get_absolute_url()


class StaticSitemap(Sitemap):
    """Sitemap for static pages."""

    changefreq = "weekly"
    priority = 0.5

    def items(self):
        return [
            'catalog:home',
            'search:app_search',
            'submissions:submit_app',
            'editorial:post_list',
            'editorial:collection_list',
            'editorial:comparison_list',
            'newsletter:subscribe',
            'newsletter:issue_archive',
        ]

    def location(self, item):
        return reverse(item)