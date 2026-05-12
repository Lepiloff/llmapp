"""Search admin configuration."""
from __future__ import annotations

from django.contrib import admin

from .models import SearchLog, PopularSearch


@admin.register(SearchLog)
class SearchLogAdmin(admin.ModelAdmin):
    list_display = ['query', 'results_count', 'created_at', 'ip_address']
    list_filter = ['created_at', 'results_count']
    search_fields = ['query']
    readonly_fields = ['created_at', 'updated_at']

    fieldsets = (
        ('Search Info', {
            'fields': ('query', 'results_count')
        }),
        ('Context', {
            'fields': ('filters', 'user_agent', 'ip_address')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def has_change_permission(self, request, obj=None):
        return False  # Read-only

    def has_add_permission(self, request):
        return False  # Read-only


@admin.register(PopularSearch)
class PopularSearchAdmin(admin.ModelAdmin):
    list_display = ['display_name', 'query', 'search_count', 'is_featured', 'sort_order']
    list_editable = ['is_featured', 'sort_order']
    list_filter = ['is_featured']
    search_fields = ['query', 'display_name']

    fieldsets = (
        ('Search Term', {
            'fields': ('query', 'display_name', 'search_count')
        }),
        ('Display', {
            'fields': ('is_featured', 'sort_order')
        }),
    )

    actions = ['mark_as_featured', 'unmark_as_featured']

    def mark_as_featured(self, request, queryset):
        count = queryset.update(is_featured=True)
        self.message_user(request, f"{count} searches marked as featured.")
    mark_as_featured.short_description = "Mark as featured"

    def unmark_as_featured(self, request, queryset):
        count = queryset.update(is_featured=False)
        self.message_user(request, f"{count} searches unmarked as featured.")
    unmark_as_featured.short_description = "Remove from featured"