"""Editorial app URLs."""
from __future__ import annotations

from django.urls import path

from . import views

app_name = 'editorial'

urlpatterns = [
    # Blog posts
    path('', views.post_list, name='post_list'),
    path('post/<slug:slug>/', views.post_detail, name='post_detail'),

    # Collections
    path('collections/', views.collection_list, name='collection_list'),
    path('collection/<slug:slug>/', views.collection_detail, name='collection_detail'),

    # Comparisons
    path('comparisons/', views.comparison_list, name='comparison_list'),
    path('comparison/<slug:slug>/', views.comparison_detail, name='comparison_detail'),
]