"""Sources admin configuration."""
from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from .models import Source, UnparsedRegistryRecord, LinkCheckResult, LinkHealth


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = [
        'app_name', 'source_type', 'external_id', 'is_primary', 'is_active',
        'fetched_at', 'last_enriched_at',
    ]
    list_filter = [
        'source_type', 'is_primary', 'is_active', 'fetched_at',
        'last_enriched_at',
    ]
    search_fields = ['app__name', 'external_id', 'source_url']
    readonly_fields = ['fetched_at']

    def app_name(self, obj):
        return obj.app.name
    app_name.short_description = 'App'
    app_name.admin_order_field = 'app__name'


@admin.register(UnparsedRegistryRecord)
class UnparsedRegistryRecordAdmin(admin.ModelAdmin):
    list_display = ['id', 'schema_version', 'error_preview', 'received_at', 'resolved_at']
    list_filter = ['schema_version', 'received_at', 'resolved_at']
    readonly_fields = ['received_at']

    fieldsets = (
        ('Record Info', {
            'fields': ('schema_version', 'received_at', 'resolved_at')
        }),
        ('Error', {
            'fields': ('error',)
        }),
        ('Payload', {
            'fields': ('payload',),
            'classes': ('collapse',)
        }),
    )

    def error_preview(self, obj):
        if obj.error:
            return obj.error[:100] + ('...' if len(obj.error) > 100 else '')
        return '-'
    error_preview.short_description = 'Error'

    actions = ['mark_as_resolved']

    def mark_as_resolved(self, request, queryset):
        from django.utils import timezone
        count = queryset.update(resolved_at=timezone.now())
        self.message_user(request, f"{count} records marked as resolved.")
    mark_as_resolved.short_description = "Mark selected records as resolved"


@admin.register(LinkCheckResult)
class LinkCheckResultAdmin(admin.ModelAdmin):
    list_display = ['app_name', 'target', 'url_preview', 'ok', 'status_code', 'checked_at']
    list_filter = ['target', 'ok', 'status_code', 'checked_at']
    search_fields = ['app__name', 'url']
    readonly_fields = ['checked_at']

    def app_name(self, obj):
        return obj.app.name
    app_name.short_description = 'App'
    app_name.admin_order_field = 'app__name'

    def url_preview(self, obj):
        return obj.url[:50] + ('...' if len(obj.url) > 50 else '')
    url_preview.short_description = 'URL'


@admin.register(LinkHealth)
class LinkHealthAdmin(admin.ModelAdmin):
    list_display = [
        'app_name', 'target', 'url_preview', 'consecutive_failures',
        'last_ok_at', 'last_failed_at'
    ]
    list_filter = ['target', 'consecutive_failures', 'last_ok_at', 'last_failed_at']
    search_fields = ['app__name', 'url']

    def app_name(self, obj):
        return obj.app.name
    app_name.short_description = 'App'
    app_name.admin_order_field = 'app__name'

    def url_preview(self, obj):
        return obj.url[:50] + ('...' if len(obj.url) > 50 else '')
    url_preview.short_description = 'URL'
