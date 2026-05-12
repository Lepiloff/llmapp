"""Newsletter app URLs."""
from __future__ import annotations

from django.urls import path

from . import views

app_name = 'newsletter'

urlpatterns = [
    # Subscription management
    path('subscribe/', views.subscribe, name='subscribe'),
    path('subscribe/success/', views.subscribe_success, name='subscribe_success'),
    path('confirm/<uuid:token>/', views.confirm_subscription, name='confirm'),
    path('unsubscribe/<uuid:token>/', views.unsubscribe, name='unsubscribe'),

    # Newsletter archive
    path('archive/', views.issue_archive, name='issue_archive'),
    path('issue/<slug:slug>/', views.issue_detail, name='issue_detail'),

    # Email tracking
    path('track/open/<int:issue_id>/<int:subscriber_id>.png', views.track_email_open, name='track_open'),
    path('track/click/<int:issue_id>/<int:subscriber_id>/', views.track_email_click, name='track_click'),
]