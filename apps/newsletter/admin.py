"""Newsletter admin configuration."""
from __future__ import annotations

from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html

from .models import EmailClick, EmailOpen, Issue, IssueApp, Subscriber


class IssueAppInline(admin.TabularInline):
    model = IssueApp
    extra = 0
    autocomplete_fields = ['app']


@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    list_display = [
        'email', 'status', 'frequency', 'source', 'created_at',
        'last_opened_at', 'total_opens', 'total_clicks'
    ]
    list_filter = ['status', 'frequency', 'source', 'created_at']
    search_fields = ['email']
    readonly_fields = [
        'confirmation_token', 'created_at', 'updated_at',
        'confirmed_at', 'unsubscribed_at', 'ip_address'
    ]

    fieldsets = (
        ('Subscription', {
            'fields': ('email', 'status', 'frequency')
        }),
        ('Confirmation', {
            'fields': ('confirmation_token', 'confirmed_at')
        }),
        ('Analytics', {
            'fields': ('source', 'last_opened_at', 'total_opens', 'total_clicks')
        }),
        ('Meta', {
            'fields': ('ip_address', 'user_agent', 'created_at', 'updated_at', 'unsubscribed_at'),
            'classes': ('collapse',)
        }),
    )

    actions = ['activate_subscribers', 'unsubscribe_subscribers']

    def activate_subscribers(self, request, queryset):
        count = queryset.filter(status=Subscriber.Status.PENDING).update(
            status=Subscriber.Status.ACTIVE,
            confirmed_at=timezone.now()
        )
        self.message_user(request, f"{count} subscribers activated.")
    activate_subscribers.short_description = "Activate selected subscribers"

    def unsubscribe_subscribers(self, request, queryset):
        count = 0
        for subscriber in queryset.filter(status=Subscriber.Status.ACTIVE):
            subscriber.unsubscribe()
            count += 1
        self.message_user(request, f"{count} subscribers unsubscribed.")
    unsubscribe_subscribers.short_description = "Unsubscribe selected subscribers"


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'status', 'scheduled_for', 'sent_at',
        'recipient_count', 'open_rate_display', 'click_rate_display'
    ]
    list_filter = ['status', 'created_at', 'sent_at']
    search_fields = ['title', 'subject_line']
    prepopulated_fields = {'slug': ('title',)}
    readonly_fields = [
        'created_at', 'updated_at', 'sent_at',
        'recipient_count', 'delivered_count', 'opened_count', 'clicked_count',
        'bounced_count', 'unsubscribed_count', 'open_rate', 'click_rate'
    ]

    fieldsets = (
        ('Content', {
            'fields': ('title', 'slug', 'subject_line', 'preheader', 'intro_text', 'conclusion_text')
        }),
        ('Publishing', {
            'fields': ('status', 'scheduled_for', 'sent_at')
        }),
        ('Statistics', {
            'fields': (
                'recipient_count', 'delivered_count', 'opened_count', 'clicked_count',
                'bounced_count', 'unsubscribed_count', 'open_rate', 'click_rate'
            ),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    inlines = [IssueAppInline]

    def open_rate_display(self, obj):
        rate = obj.open_rate
        color = '#28a745' if rate > 20 else '#ffc107' if rate > 10 else '#dc3545'
        return format_html('<span style="color: {}">{:.1f}%</span>', color, rate)
    open_rate_display.short_description = 'Open Rate'

    def click_rate_display(self, obj):
        rate = obj.click_rate
        color = '#28a745' if rate > 3 else '#ffc107' if rate > 1 else '#dc3545'
        return format_html('<span style="color: {}">{:.1f}%</span>', color, rate)
    click_rate_display.short_description = 'Click Rate'

    actions = ['schedule_issues', 'send_test_issue']

    def schedule_issues(self, request, queryset):
        # Schedule for tomorrow at 10 AM
        from datetime import timedelta
        tomorrow_10am = (timezone.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        count = queryset.filter(status=Issue.Status.DRAFT).update(
            status=Issue.Status.SCHEDULED,
            scheduled_for=tomorrow_10am
        )
        self.message_user(request, f"{count} issues scheduled for tomorrow 10 AM.")
    schedule_issues.short_description = "Schedule for tomorrow 10 AM"

    def send_test_issue(self, request, queryset):
        for _issue in queryset.filter(status=Issue.Status.DRAFT):
            # Create a test issue copy
            # This would need implementation
            pass
        self.message_user(request, "Test functionality not implemented yet.")
    send_test_issue.short_description = "Send test issue"


@admin.register(EmailClick)
class EmailClickAdmin(admin.ModelAdmin):
    list_display = ['issue_title', 'subscriber_email', 'link_type', 'target_url_preview', 'created_at']
    list_filter = ['link_type', 'created_at']
    search_fields = ['issue__title', 'subscriber__email', 'target_url']
    readonly_fields = ['created_at', 'updated_at']

    def issue_title(self, obj):
        return obj.issue.title
    issue_title.short_description = 'Issue'

    def subscriber_email(self, obj):
        return obj.subscriber.email
    subscriber_email.short_description = 'Subscriber'

    def target_url_preview(self, obj):
        return obj.target_url[:50] + ('...' if len(obj.target_url) > 50 else '')
    target_url_preview.short_description = 'URL'

    def has_change_permission(self, request, obj=None):
        return False  # Read-only

    def has_add_permission(self, request):
        return False  # Read-only


@admin.register(EmailOpen)
class EmailOpenAdmin(admin.ModelAdmin):
    list_display = ['issue_title', 'subscriber_email', 'created_at']
    list_filter = ['created_at']
    search_fields = ['issue__title', 'subscriber__email']
    readonly_fields = ['created_at', 'updated_at']

    def issue_title(self, obj):
        return obj.issue.title
    issue_title.short_description = 'Issue'

    def subscriber_email(self, obj):
        return obj.subscriber.email
    subscriber_email.short_description = 'Subscriber'

    def has_change_permission(self, request, obj=None):
        return False  # Read-only

    def has_add_permission(self, request):
        return False  # Read-only
