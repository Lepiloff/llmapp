"""Editorial content views."""
from __future__ import annotations

from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import cache_page

from .models import Collection, Comparison, Post, Tag


@cache_page(60 * 30, key_prefix="blog_index_v1")
def post_list(request: HttpRequest) -> HttpResponse:
    """Blog post listing page."""
    posts_qs = Post.objects.filter(
        status=Post.Status.PUBLISHED,
        published_at__lte=timezone.now()
    ).select_related('author')

    # Filter by tag if specified
    tag_slug = request.GET.get('tag')
    if tag_slug:
        tag = get_object_or_404(Tag, slug=tag_slug)
        posts_qs = posts_qs.filter(tags=tag)
    else:
        tag = None

    # Filter by post type if specified
    post_type = request.GET.get('type')
    if post_type and post_type in [choice[0] for choice in Post.PostType.choices]:
        posts_qs = posts_qs.filter(post_type=post_type)

    # Pagination
    paginator = Paginator(posts_qs, 12)
    page = paginator.get_page(request.GET.get('page', 1))

    # Featured posts for sidebar
    featured_posts = Post.objects.filter(
        status=Post.Status.PUBLISHED,
        featured_until__gt=timezone.now()
    )[:5]

    # Popular tags
    popular_tags = Tag.objects.filter(
        posts__status=Post.Status.PUBLISHED
    ).distinct()[:10]

    context = {
        'page_obj': page,
        'current_tag': tag,
        'current_type': post_type,
        'featured_posts': featured_posts,
        'popular_tags': popular_tags,
        'post_types': Post.PostType.choices,
    }

    return render(request, 'editorial/post_list.html', context)


def post_detail(request: HttpRequest, slug: str) -> HttpResponse:
    """Individual blog post page."""
    post = get_object_or_404(
        Post.objects.filter(
            status=Post.Status.PUBLISHED,
            published_at__lte=timezone.now()
        ).select_related('author').prefetch_related('tags', 'related_apps'),
        slug=slug
    )

    # Increment view count
    post.increment_view_count()

    # Related posts
    related_posts = Post.objects.filter(
        status=Post.Status.PUBLISHED,
        published_at__lte=timezone.now(),
        post_type=post.post_type
    ).exclude(id=post.id)[:3]

    context = {
        'post': post,
        'related_posts': related_posts,
    }

    return render(request, 'editorial/post_detail.html', context)


@cache_page(60 * 60, key_prefix="collections_v1")
def collection_list(request: HttpRequest) -> HttpResponse:
    """List of curated app collections."""
    collections = Collection.objects.filter(
        is_published=True
    ).select_related('curator')

    context = {
        'collections': collections,
    }

    return render(request, 'editorial/collection_list.html', context)


def collection_detail(request: HttpRequest, slug: str) -> HttpResponse:
    """Individual collection page."""
    collection = get_object_or_404(
        Collection.objects.filter(is_published=True).select_related('curator'),
        slug=slug
    )

    # Get apps in the collection with their descriptions
    collection_apps = (
        collection.collectionapp_set
        .select_related('app')
        .prefetch_related('app__platforms', 'app__categories')
        .order_by('sort_order')
    )

    context = {
        'collection': collection,
        'collection_apps': collection_apps,
    }

    return render(request, 'editorial/collection_detail.html', context)


@cache_page(60 * 60, key_prefix="comparisons_v1")
def comparison_list(request: HttpRequest) -> HttpResponse:
    """List of app comparisons."""
    comparisons = Comparison.objects.filter(
        is_published=True
    ).select_related('primary_app', 'secondary_app', 'author')

    context = {
        'comparisons': comparisons,
    }

    return render(request, 'editorial/comparison_list.html', context)


def comparison_detail(request: HttpRequest, slug: str) -> HttpResponse:
    """Individual comparison page."""
    comparison = get_object_or_404(
        Comparison.objects.filter(is_published=True).select_related(
            'primary_app', 'secondary_app', 'author'
        ),
        slug=slug
    )

    context = {
        'comparison': comparison,
    }

    return render(request, 'editorial/comparison_detail.html', context)