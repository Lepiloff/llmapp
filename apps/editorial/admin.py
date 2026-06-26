"""Editorial admin configuration."""
from __future__ import annotations

from django.contrib import admin
from django.utils import timezone

from .models import Collection, CollectionApp, Comparison, Post, PostApp, Tag


class PostAppInline(admin.TabularInline):
    model = PostApp
    extra = 0
    autocomplete_fields = ['app']


class CollectionAppInline(admin.TabularInline):
    model = CollectionApp
    extra = 0
    autocomplete_fields = ['app']


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'post_type', 'status', 'author', 'published_at',
        'is_featured', 'view_count'
    ]
    list_filter = ['post_type', 'status', 'published_at', 'featured_until']
    search_fields = ['title', 'subtitle', 'content']
    prepopulated_fields = {'slug': ('title',)}
    readonly_fields = ['created_at', 'updated_at', 'view_count']
    filter_horizontal = ['tags']

    fieldsets = (
        ('Content', {
            'fields': ('title', 'slug', 'subtitle', 'post_type', 'excerpt', 'content')
        }),
        ('Media', {
            'fields': ('cover_image', 'cover_alt_text')
        }),
        ('Publishing', {
            'fields': ('status', 'author', 'published_at', 'featured_until')
        }),
        ('SEO', {
            'fields': ('meta_title', 'meta_description'),
            'classes': ('collapse',)
        }),
        ('Tags', {
            'fields': ('tags',)
        }),
        ('Stats', {
            'fields': ('view_count', 'reading_time_minutes'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    inlines = [PostAppInline]

    actions = ['publish_posts', 'mark_as_featured']

    def publish_posts(self, request, queryset):
        now = timezone.now()
        count = 0
        for post in queryset.filter(status=Post.Status.DRAFT):
            post.status = Post.Status.PUBLISHED
            if not post.published_at:
                post.published_at = now
            post.save()
            count += 1
        self.message_user(request, f"{count} posts published.")
    publish_posts.short_description = "Publish selected posts"

    def mark_as_featured(self, request, queryset):
        # Feature for 7 days
        featured_until = timezone.now() + timezone.timedelta(days=7)
        count = queryset.update(featured_until=featured_until)
        self.message_user(request, f"{count} posts marked as featured for 7 days.")
    mark_as_featured.short_description = "Feature for 7 days"


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ['name', 'curator', 'is_published', 'created_at']
    list_filter = ['is_published', 'created_at']
    search_fields = ['name', 'description']
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ['created_at', 'updated_at']

    fieldsets = (
        ('Content', {
            'fields': ('name', 'slug', 'subtitle', 'description', 'intro_text', 'conclusion_text')
        }),
        ('Media', {
            'fields': ('cover_image',)
        }),
        ('Publishing', {
            'fields': ('is_published', 'curator')
        }),
        ('SEO', {
            'fields': ('meta_title', 'meta_description'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    inlines = [CollectionAppInline]

    actions = ['publish_collections']

    def publish_collections(self, request, queryset):
        count = queryset.update(is_published=True)
        self.message_user(request, f"{count} collections published.")
    publish_collections.short_description = "Publish selected collections"


@admin.register(Comparison)
class ComparisonAdmin(admin.ModelAdmin):
    list_display = ['title', 'primary_app', 'secondary_app', 'author', 'is_published', 'created_at']
    list_filter = ['is_published', 'created_at']
    search_fields = ['title', 'introduction', 'conclusion']
    prepopulated_fields = {'slug': ('title',)}
    readonly_fields = ['created_at', 'updated_at']
    autocomplete_fields = ['primary_app', 'secondary_app']

    fieldsets = (
        ('Content', {
            'fields': ('title', 'slug', 'introduction', 'conclusion')
        }),
        ('Apps', {
            'fields': ('primary_app', 'secondary_app')
        }),
        ('Analysis', {
            'fields': ('criteria',)
        }),
        ('Publishing', {
            'fields': ('is_published', 'author')
        }),
        ('SEO', {
            'fields': ('meta_title', 'meta_description'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    actions = ['publish_comparisons']

    def publish_comparisons(self, request, queryset):
        count = queryset.update(is_published=True)
        self.message_user(request, f"{count} comparisons published.")
    publish_comparisons.short_description = "Publish selected comparisons"


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']
    search_fields = ['name']
    prepopulated_fields = {'slug': ('name',)}