"""Analytics admin configuration."""
from __future__ import annotations

from django.contrib import admin

from .models import ClickEvent, PageView, TrendingScore


@admin.register(ClickEvent)
class ClickEventAdmin(admin.ModelAdmin):
    list_display = ['app_name', 'link_type', 'source_page', 'source_position', 'created_at']
    list_filter = ['link_type', 'source_page', 'created_at']
    search_fields = ['app__name']
    readonly_fields = ['created_at', 'updated_at']

    fieldsets = (
        ('Click Info', {
            'fields': ('app', 'link_type', 'source_page', 'source_position')
        }),
        ('Context', {
            'fields': ('user_agent', 'ip_address', 'referer', 'session_key')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def app_name(self, obj):
        return obj.app.name
    app_name.short_description = 'App'
    app_name.admin_order_field = 'app__name'

    def has_change_permission(self, request, obj=None):
        return False  # Read-only

    def has_add_permission(self, request):
        return False  # Read-only


@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ['page_type', 'page_identifier', 'load_time_ms', 'created_at']
    list_filter = ['page_type', 'created_at']
    search_fields = ['page_identifier']
    readonly_fields = ['created_at', 'updated_at']

    fieldsets = (
        ('Page Info', {
            'fields': ('page_type', 'page_identifier', 'load_time_ms')
        }),
        ('Context', {
            'fields': ('user_agent', 'ip_address', 'referer', 'session_key')
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


@admin.register(TrendingScore)
class TrendingScoreAdmin(admin.ModelAdmin):
    list_display = ['app_name', 'score_1d', 'score_7d', 'score_30d', 'last_calculated_at']
    list_filter = ['last_calculated_at']
    search_fields = ['app__name']
    readonly_fields = ['last_calculated_at']

    fieldsets = (
        ('App', {
            'fields': ('app',)
        }),
        ('Scores', {
            'fields': ('score_1d', 'score_7d', 'score_30d', 'last_calculated_at')
        }),
    )

    def app_name(self, obj):
        return obj.app.name
    app_name.short_description = 'App'
    app_name.admin_order_field = 'app__name'

    actions = ['recalculate_scores']

    def recalculate_scores(self, request, queryset):
        from .tasks import calculate_trending_scores
        calculate_trending_scores.delay()
        self.message_user(request, "Trending score recalculation queued.")
    recalculate_scores.short_description = "Recalculate trending scores"