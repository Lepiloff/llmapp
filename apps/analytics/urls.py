"""Analytics app URLs."""
from __future__ import annotations

from django.urls import path

from . import views

app_name = 'analytics'

urlpatterns = [
    path('<slug:slug>/', views.outbound_redirect, name='outbound_redirect'),
]