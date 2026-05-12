"""SEO admin configuration."""
from __future__ import annotations

from django.contrib import admin

from .models import SeoPage, Redirect


@admin.register(SeoPage)
class SeoPageAdmin(admin.ModelAdmin):
    list_display = ['page_type', 'page_identifier', 'meta_title', 'is_active', 'created_at']
    list_filter = ['page_type', 'is_active', 'created_at']
    search_fields = ['page_identifier', 'meta_title', 'meta_description']

    fieldsets = (
        ('Page', {
            'fields': ('page_type', 'page_identifier', 'is_active')
        }),
        ('SEO Meta', {
            'fields': ('meta_title', 'meta_description', 'meta_keywords')
        }),
        ('Social Media', {
            'fields': ('og_title', 'og_description', 'og_image', 'twitter_title', 'twitter_description'),
            'classes': ('collapse',)
        }),
        ('Structured Data', {
            'fields': ('json_ld',),
            'classes': ('collapse',)
        }),
    )

    actions = ['activate_pages', 'deactivate_pages']

    def activate_pages(self, request, queryset):
        count = queryset.update(is_active=True)
        self.message_user(request, f"{count} SEO pages activated.")
    activate_pages.short_description = "Activate selected pages"

    def deactivate_pages(self, request, queryset):
        count = queryset.update(is_active=False)
        self.message_user(request, f"{count} SEO pages deactivated.")
    deactivate_pages.short_description = "Deactivate selected pages"


@admin.register(Redirect)
class RedirectAdmin(admin.ModelAdmin):
    list_display = ['from_path', 'to_path', 'status_code', 'is_active', 'hit_count', 'created_at']
    list_filter = ['status_code', 'is_active', 'created_at']
    search_fields = ['from_path', 'to_path']
    readonly_fields = ['hit_count', 'created_at', 'updated_at']

    fieldsets = (
        ('Redirect', {
            'fields': ('from_path', 'to_path', 'status_code', 'is_active')
        }),
        ('Stats', {
            'fields': ('hit_count', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    actions = ['activate_redirects', 'deactivate_redirects']

    def activate_redirects(self, request, queryset):
        count = queryset.update(is_active=True)
        self.message_user(request, f"{count} redirects activated.")
    activate_redirects.short_description = "Activate selected redirects"

    def deactivate_redirects(self, request, queryset):
        count = queryset.update(is_active=False)
        self.message_user(request, f"{count} redirects deactivated.")
    deactivate_redirects.short_description = "Deactivate selected redirects"