"""Catalog URLs.

The faceted `/apps/` list lives in `apps.search.urls`; this module owns the
home page and the lazy-loaded "newly added" HTMX strip.

The detail/category dispatcher at `/apps/<slug>/` is registered in
`config/urls.py` because it has to coexist with other top-level routes
(`/apps/`, `/<platform>/`, `/<platform>/<category>/`) whose ordering
matters for URL resolution.
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.home, name="home"),
    path("apps/newly-added/", views.newly_added, name="newly_added"),
]
