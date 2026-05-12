"""Catalog URLs.

The faceted `/apps/` list lives in `apps.search.urls`; this module owns the
static slices (home, detail, platform/category/cross pages).

The order of `path()` entries matters: detail's `<slug:slug>` would shadow
`<slug:category_slug>` if registered first, so detail is wired last.
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.home, name="home"),
    path("apps/newly-added/", views.newly_added, name="newly_added"),
    # /apps/<category_slug>/ is registered in config/urls.py with an explicit
    # path so the detail route below stays unambiguous when slugs collide.
    path("apps/<slug:slug>/", views.app_detail, name="detail"),
]
