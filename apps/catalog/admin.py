"""Django Admin — the editor's primary moderation UI.

Architecture ref: docs/architecture.md § 14.
"""
from __future__ import annotations

from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    App,
    AppCapability,
    AppCategory,
    AppPlatform,
    AppUseCase,
    Capability,
    Category,
    ListingType,
    Platform,
    UseCase,
)
from .services import recalc_quality_score, transition_to_published


# ---------------------------------------------------------------------------
# Inlines
# ---------------------------------------------------------------------------
class AppPlatformInline(admin.TabularInline):
    model = AppPlatform
    extra = 0
    autocomplete_fields = ["platform"]
    fields = (
        "platform",
        "compatibility_status",
        "supported_plans",
        "region_availability",
        "scope_summary",
        "official_directory_url",
        "install_url",
        "metadata",
        "last_verified_on_platform_at",
    )


class AppCategoryInline(admin.TabularInline):
    model = AppCategory
    extra = 0
    autocomplete_fields = ["category"]


class AppCapabilityInline(admin.TabularInline):
    model = AppCapability
    extra = 0
    autocomplete_fields = ["capability"]


class AppUseCaseInline(admin.TabularInline):
    model = AppUseCase
    extra = 0
    autocomplete_fields = ["use_case"]


# ---------------------------------------------------------------------------
# Reference admins
# ---------------------------------------------------------------------------
@admin.register(Platform)
class PlatformAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "public_path", "sort_order")
    search_fields = ("name", "slug", "public_path")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("sort_order", "name")


@admin.register(ListingType)
class ListingTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "sort_order")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("sort_order", "name")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "sort_order")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("sort_order", "name")


@admin.register(Capability)
class CapabilityAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "sort_order")
    search_fields = ("key", "label")
    ordering = ("sort_order", "key")


@admin.register(UseCase)
class UseCaseAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "app_count")
    search_fields = ("title", "slug")
    prepopulated_fields = {"slug": ("title",)}
    actions = ["merge_into_target"]

    def get_queryset(self, request):
        from django.db.models import Count

        return super().get_queryset(request).annotate(_app_count=Count("apps"))

    def app_count(self, obj) -> int:
        return getattr(obj, "_app_count", 0)
    app_count.admin_order_field = "_app_count"
    app_count.short_description = "Apps"

    @admin.action(description="Merge selected use-cases into one canonical row")
    def merge_into_target(self, request, queryset):
        """Two-step admin action: pick target slug, then collapse the rest.

        Step 1 (first click): the action shows an intermediate page
        with the selected use-cases as a radio list — editor picks
        which one is canonical.
        Step 2 (form POST): ``merge_use_cases`` re-points every
        AppUseCase from the non-target rows onto the target, then
        deletes the source rows.
        """
        from django.shortcuts import render
        from django.http import HttpResponseRedirect

        from .services import merge_use_cases

        ids = list(queryset.values_list("pk", flat=True))
        if len(ids) < 2:
            self.message_user(
                request,
                "Select at least two use-cases to merge.",
                level=messages.WARNING,
            )
            return None

        target_id = request.POST.get("target")
        if target_id and request.POST.get("post") == "yes":
            try:
                target_id = int(target_id)
            except (TypeError, ValueError):
                self.message_user(
                    request, "Invalid target selection.", level=messages.ERROR,
                )
                return None
            if target_id not in ids:
                self.message_user(
                    request,
                    "Target must be one of the selected rows.",
                    level=messages.ERROR,
                )
                return None

            stats = merge_use_cases(target_id, [pk for pk in ids if pk != target_id])
            target = UseCase.objects.get(pk=target_id)
            self.message_user(
                request,
                (
                    f"Merged into '{target.title}': "
                    f"{stats['reassigned']} AppUseCase rows re-pointed, "
                    f"{stats['deduplicated']} duplicates removed, "
                    f"{stats['deleted_use_cases']} source use-cases deleted."
                ),
                level=messages.SUCCESS,
            )
            return HttpResponseRedirect(request.get_full_path())

        context = {
            **self.admin_site.each_context(request),
            "title": "Merge use-cases",
            "queryset": queryset,
            "opts": self.model._meta,
            "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        }
        return render(request, "admin/catalog/usecase/merge_confirm.html", context)


# ---------------------------------------------------------------------------
# App admin
# ---------------------------------------------------------------------------
@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "status",
        "platform_verification_status",
        "editorial_review_status",
        "developer_claim_status",
        "quality_score",
        "is_featured",
        "last_checked_at",
    )
    list_filter = (
        "status",
        "platform_verification_status",
        "editorial_review_status",
        "developer_claim_status",
        "launch_status",
        "pricing_model",
        "is_featured",
    )
    search_fields = (
        "name",
        "slug",
        "developer_name",
        "short_description",
    )
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ["listing_types"]
    inlines = [
        AppPlatformInline,
        AppCategoryInline,
        AppCapabilityInline,
        AppUseCaseInline,
    ]
    actions = [
        "action_publish",
        "action_mark_editorial_reviewed",
        "action_mark_platform_official",
        "action_recalculate_quality",
        "action_refresh_search_vector",
    ]
    readonly_fields = (
        "first_seen_at",
        "last_checked_at",
        "quality_score",
        "created_at",
        "updated_at",
        "publish_checklist_html",
    )
    fieldsets = (
        (None, {"fields": ("name", "slug", "listing_types")}),
        ("Descriptions", {
            "fields": ("short_description", "long_description", "verdict"),
        }),
        ("Developer", {"fields": ("developer_name", "developer_url", "contact_email")}),
        ("Media", {"fields": ("logo", "cover_image")}),
        ("Links", {"fields": ("official_page_url", "install_url", "repo_url")}),
        ("Status — three trust axes", {
            "fields": (
                "status",
                "platform_verification_status",
                "editorial_review_status",
                "developer_claim_status",
                "launch_status",
                "pricing_model",
            ),
            "description": (
                "Trust axes are independent. Do not collapse them. "
                "See business.md § 6.5."
            ),
        }),
        ("Flags", {"fields": ("is_featured", "is_indexable")}),
        ("SEO", {"fields": ("meta_title", "meta_description")}),
        ("Quality / publish gate", {"fields": ("quality_score", "publish_checklist_html")}),
        ("Timestamps", {"fields": ("first_seen_at", "last_checked_at", "created_at", "updated_at")}),
    )

    # ------- helpers -------
    def get_queryset(self, request):
        # admin list view: prefetch trust-axis source data to avoid N+1
        qs = super().get_queryset(request)
        return qs.prefetch_related("platform_links", "categories")

    @admin.display(description="Publish checklist")
    def publish_checklist_html(self, obj: App) -> str:
        from .services import get_publish_checklist

        if not obj.pk:
            return "Save the draft first to see the checklist."
        rows = []
        for item in get_publish_checklist(obj):
            color = "#198754" if item["ok"] else "#dc3545"
            symbol = "✓" if item["ok"] else "✗"
            rows.append(
                f'<li style="color:{color}">{symbol} {item["label"]}</li>'
            )
        return format_html(
            "<ul style='margin:0; padding-left:1em;'>{}</ul>", "".join(rows)
        )

    # ------- actions -------
    @admin.action(description="Publish selected (with validation)")
    def action_publish(self, request, queryset):
        succeeded, failed = 0, []
        for app in queryset:
            try:
                transition_to_published(app, request.user)
                succeeded += 1
            except ValueError as exc:
                failed.append(f"{app.name}: {exc}")
        if failed:
            self.message_user(
                request, "; ".join(failed), level=messages.WARNING
            )
        self.message_user(request, f"Published {succeeded}")

    @admin.action(description="Mark as editorially reviewed")
    def action_mark_editorial_reviewed(self, request, queryset):
        queryset.update(
            editorial_review_status=App.EditorialReviewStatus.REVIEWED,
            last_checked_at=timezone.now(),
        )
        self.message_user(request, f"Reviewed {queryset.count()}")

    @admin.action(
        description="Mark as listed in official platform directory (requires directory URL)"
    )
    def action_mark_platform_official(self, request, queryset):
        # Only flip rows that actually have a directory URL: a bare flag
        # would lie to users and fail the publish gate later.
        ok = queryset.filter(
            platform_links__official_directory_url__gt=""
        ).distinct()
        ok.update(
            platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
            last_checked_at=timezone.now(),
        )
        skipped = queryset.exclude(pk__in=ok.values("pk")).count()
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} app(s) without an official_directory_url.",
                level=messages.WARNING,
            )

    @admin.action(description="Recalculate quality score")
    def action_recalculate_quality(self, request, queryset):
        for app in queryset:
            recalc_quality_score(app)
        self.message_user(request, f"Recalculated {queryset.count()}")

    @admin.action(description="Refresh search vector")
    def action_refresh_search_vector(self, request, queryset):
        from apps.search.tasks import refresh_search_vector_task

        for app_id in queryset.values_list("pk", flat=True):
            refresh_search_vector_task.delay(app_id)
        self.message_user(request, f"Queued {queryset.count()} refresh task(s).")
