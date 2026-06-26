"""Submissions admin configuration."""
from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from .models import ClaimRequest, Submission


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = [
        'app_name', 'developer_email', 'status', 'created_at', 'reviewed_at'
    ]
    list_filter = ['status', 'created_at', 'reviewed_at']
    search_fields = ['app_name', 'developer_name', 'developer_email', 'short_description']
    readonly_fields = ['created_at', 'updated_at', 'submitter_ip', 'turnstile_token']

    fieldsets = (
        ('App Info', {
            'fields': ('app_name', 'short_description', 'long_description')
        }),
        ('Developer', {
            'fields': ('developer_name', 'developer_email')
        }),
        ('Links', {
            'fields': ('official_url', 'install_url', 'repo_url')
        }),
        ('Platform Info', {
            'fields': ('suggested_platforms', 'suggested_categories')
        }),
        ('Review', {
            'fields': ('status', 'reviewer_notes', 'reviewed_at', 'created_app')
        }),
        ('Meta', {
            'fields': ('created_at', 'updated_at', 'submitter_ip', 'turnstile_token'),
            'classes': ('collapse',)
        }),
    )

    actions = ['approve_submissions', 'reject_submissions']

    def approve_submissions(self, request, queryset):
        count = queryset.filter(status=Submission.Status.PENDING).update(
            status=Submission.Status.APPROVED
        )
        self.message_user(request, f"{count} submissions approved.")
    approve_submissions.short_description = "Approve selected submissions"

    def reject_submissions(self, request, queryset):
        count = queryset.filter(status=Submission.Status.PENDING).update(
            status=Submission.Status.REJECTED
        )
        self.message_user(request, f"{count} submissions rejected.")
    reject_submissions.short_description = "Reject selected submissions"


@admin.register(ClaimRequest)
class ClaimRequestAdmin(admin.ModelAdmin):
    list_display = [
        'app_name_link', 'claimant_email', 'status', 'verification_method', 'created_at'
    ]
    list_filter = ['status', 'verification_method', 'created_at']
    search_fields = ['app__name', 'claimant_name', 'claimant_email']
    readonly_fields = ['created_at', 'updated_at', 'submitter_ip', 'turnstile_token']

    fieldsets = (
        ('Claim Info', {
            'fields': ('app', 'claimant_name', 'claimant_email')
        }),
        ('Verification', {
            'fields': ('verification_method', 'evidence')
        }),
        ('Review', {
            'fields': ('status', 'reviewer_notes', 'reviewed_at')
        }),
        ('Meta', {
            'fields': ('created_at', 'updated_at', 'submitter_ip', 'turnstile_token'),
            'classes': ('collapse',)
        }),
    )

    def app_name_link(self, obj):
        return format_html(
            '<a href="{}">{}</a>',
            f"/admin/catalog/app/{obj.app.pk}/change/",
            obj.app.name
        )
    app_name_link.short_description = 'App'
    app_name_link.admin_order_field = 'app__name'

    actions = ['approve_claims', 'reject_claims']

    def approve_claims(self, request, queryset):
        approved = 0
        for claim in queryset.filter(status=ClaimRequest.Status.PENDING):
            claim.mark_reviewed(ClaimRequest.Status.APPROVED, "Approved via admin action")
            approved += 1
        self.message_user(request, f"{approved} claims approved.")
    approve_claims.short_description = "Approve selected claims"

    def reject_claims(self, request, queryset):
        count = queryset.filter(status=ClaimRequest.Status.PENDING).update(
            status=ClaimRequest.Status.REJECTED
        )
        self.message_user(request, f"{count} claims rejected.")
    reject_claims.short_description = "Reject selected claims"