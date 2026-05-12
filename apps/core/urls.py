"""Core app URLs - health checks and utilities."""
from __future__ import annotations

from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('', views.health_check, name='health_check'),
    path('db/', views.health_check_db, name='health_check_db'),
    path('cache/', views.health_check_cache, name='health_check_cache'),
]