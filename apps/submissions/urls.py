"""URLs for user submissions and claim requests."""
from __future__ import annotations

from django.urls import path

from . import views

app_name = 'submissions'

urlpatterns = [
    # App submission
    path('', views.submit_app, name='submit_app'),
    path('success/', views.submit_success, name='submit_success'),

    # App claiming
    path('claim/<slug:slug>/', views.claim_app, name='claim_app'),
    path('claim/<slug:slug>/success/', views.claim_success, name='claim_success'),
]