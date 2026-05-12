"""Search app URLs."""
from __future__ import annotations

from django.urls import path

from . import views

app_name = 'search'

urlpatterns = [
    path('', views.app_search, name='app_search'),
    path('suggestions/', views.search_suggestions, name='suggestions'),
]