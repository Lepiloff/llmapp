"""Main URL configuration for LLM App Market.

Architecture refs:
  * docs/architecture.md § 7 (URL patterns)
  * docs/architecture.md § 8 (API endpoints)
"""
from __future__ import annotations

from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path
from django.views.generic import TemplateView
from django.conf import settings
from django.conf.urls.static import static

from apps.catalog import views as catalog_views
from apps.search import views as search_views

from apps.seo.sitemaps import AppsSitemap, CategoriesSitemap, PlatformsSitemap, StaticSitemap

sitemaps = {
    'apps': AppsSitemap,
    'categories': CategoriesSitemap,
    'platforms': PlatformsSitemap,
    'static': StaticSitemap,
}

urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),

    # Health checks
    path('health/', include('apps.core.urls')),

    # Main catalog pages
    path('', include('apps.catalog.urls')),

    # All specific routes must be BEFORE the catch-all platform pattern!
    path('search/', include('apps.search.urls')),
    path('apps/', search_views.app_search, name='apps_search'),
    # Single dispatcher: Category wins on slug collision, falls back to App detail.
    # See apps.catalog.views.app_or_category_detail.
    path('apps/<slug:slug>/', catalog_views.app_or_category_detail, name='catalog_item'),
    path('submit/', include('apps.submissions.urls')),
    path('blog/', include('apps.editorial.urls')),
    path('newsletter/', include('apps.newsletter.urls')),
    path('go/', include('apps.analytics.urls')),

    # Platform and category pages (catch-all — must be LAST)
    path('<slug:public_path>/', catalog_views.platform_page, name='platform_page'),
    path('<slug:public_path>/<slug:category_slug>/', catalog_views.cross_page, name='cross_page'),

    # SEO
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='sitemap'),
    path('robots.txt', TemplateView.as_view(
        template_name='seo/robots.txt',
        content_type='text/plain'
    )),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

# Customize admin
admin.site.site_header = "LLM App Market Admin"
admin.site.site_title = "LLM App Market"
admin.site.index_title = "Welcome to LLM App Market Administration"