"""Newsletter background tasks."""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Count, Q
from django.template.loader import render_to_string
from django.utils import timezone

from apps.catalog.models import App

from .models import Issue, IssueApp, Subscriber

logger = logging.getLogger(__name__)


@shared_task
def send_newsletter_issue(issue_id: int) -> dict[str, int]:
    """Send newsletter issue to all active subscribers."""
    try:
        issue = Issue.objects.get(id=issue_id, status=Issue.Status.SCHEDULED)
    except Issue.DoesNotExist:
        logger.error(f"Issue {issue_id} not found or not scheduled")
        return {'error': 'Issue not found or not scheduled'}

    # Get active subscribers
    subscribers = Subscriber.objects.filter(status=Subscriber.Status.ACTIVE)
    total_subscribers = subscribers.count()

    if total_subscribers == 0:
        logger.warning("No active subscribers found")
        return {'sent': 0, 'errors': 0, 'total': 0}

    # Update issue recipient count
    issue.recipient_count = total_subscribers
    issue.save(update_fields=['recipient_count'])

    sent_count = 0
    error_count = 0

    # Send emails in batches
    batch_size = 50
    for i in range(0, total_subscribers, batch_size):
        batch = subscribers[i:i + batch_size]

        for subscriber in batch:
            try:
                send_issue_email(issue, subscriber)
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send newsletter to {subscriber.email}: {e}")
                error_count += 1

        # Small delay between batches to avoid overwhelming the email service
        if i + batch_size < total_subscribers:
            import time
            time.sleep(1)

    # Update issue statistics
    issue.delivered_count = sent_count
    issue.mark_as_sent()

    result = {
        'sent': sent_count,
        'errors': error_count,
        'total': total_subscribers,
    }

    logger.info(f"Newsletter issue {issue_id} sending completed: {result}")
    return result


@shared_task
def create_weekly_draft() -> dict[str, any]:
    """Create a draft newsletter issue with trending apps.

    This task runs weekly to prepare a draft newsletter that editors can review and send.
    """
    now = timezone.now()
    week_ago = now - timedelta(days=7)

    try:
        # Check if we already have a draft for this week
        existing_draft = Issue.objects.filter(
            status=Issue.Status.DRAFT,
            created_at__gte=week_ago
        ).first()

        if existing_draft:
            logger.info(f"Draft already exists for this week: {existing_draft.id}")
            return {'draft_id': existing_draft.id, 'created': False}

        # Get trending apps from the last week
        trending_apps = (
            App.published.all()
            .for_listing()
            .annotate(
                click_count=Count('clicks', filter=Q(clicks__created_at__gte=week_ago))
            )
            .filter(click_count__gt=0)
            .order_by('-click_count', '-quality_score')[:10]
        )

        # Get newly added apps
        new_apps = (
            App.published.all()
            .for_listing()
            .filter(first_seen_at__gte=week_ago)
            .order_by('-quality_score')[:5]
        )

        # Create the draft issue
        week_start = now.strftime("%B %d")
        issue = Issue.objects.create(
            title=f"Weekly Roundup - {week_start}",
            slug=f"weekly-{now.strftime('%Y-%m-%d')}",
            subject_line=f"🚀 This Week in LLM Apps - {week_start}",
            preheader="Discover the hottest new apps and trending tools",
            intro_text=f"""
Here are the most popular LLM apps and tools from the week of {week_start}.
            """.strip(),
            conclusion_text="""
That's all for this week! Keep exploring and building amazing things with AI.
            """.strip(),
            status=Issue.Status.DRAFT,
        )

        # Add trending apps to the issue
        for i, app in enumerate(trending_apps):
            IssueApp.objects.create(
                issue=issue,
                app=app,
                sort_order=i + 1,
                description=f"Trending app with {app.click_count} clicks this week."
            )

        # Add new apps to the issue
        for i, app in enumerate(new_apps):
            IssueApp.objects.create(
                issue=issue,
                app=app,
                sort_order=len(trending_apps) + i + 1,
                description="New app added to the catalog this week."
            )

        result = {
            'draft_id': issue.id,
            'created': True,
            'trending_apps': len(trending_apps),
            'new_apps': len(new_apps),
        }

        logger.info(f"Weekly newsletter draft created: {result}")
        return result

    except Exception as e:
        logger.error(f"Failed to create weekly newsletter draft: {e}")
        raise


@shared_task
def cleanup_old_newsletter_data(days_to_keep: int = 90) -> dict[str, int]:
    """Clean up old newsletter tracking data."""
    cutoff_date = timezone.now() - timedelta(days=days_to_keep)

    try:
        from .models import EmailClick, EmailOpen

        # Delete old email clicks
        deleted_clicks = EmailClick.objects.filter(created_at__lt=cutoff_date).delete()[0]

        # Delete old email opens
        deleted_opens = EmailOpen.objects.filter(created_at__lt=cutoff_date).delete()[0]

        result = {
            'deleted_clicks': deleted_clicks,
            'deleted_opens': deleted_opens,
        }

        logger.info(f"Newsletter data cleanup completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Newsletter data cleanup failed: {e}")
        raise


def send_issue_email(issue: Issue, subscriber: Subscriber) -> None:
    """Send newsletter issue email to a single subscriber."""
    # Get featured apps for this issue
    issue_apps = (
        issue.issueapp_set
        .select_related('app')
        .prefetch_related('app__platforms', 'app__categories')
        .order_by('sort_order')
    )

    # Generate tracking URLs
    open_tracking_url = f"{settings.SITE_BASE_URL}/newsletter/track/open/{issue.id}/{subscriber.id}.png"
    click_tracking_base = f"{settings.SITE_BASE_URL}/newsletter/track/click/{issue.id}/{subscriber.id}/"

    # Render email content
    html_content = render_to_string('newsletter/emails/issue.html', {
        'issue': issue,
        'issue_apps': issue_apps,
        'subscriber': subscriber,
        'unsubscribe_url': f"{settings.SITE_BASE_URL}{subscriber.get_unsubscribe_url()}",
        'open_tracking_url': open_tracking_url,
        'click_tracking_base': click_tracking_base,
        'site_name': settings.SITE_NAME,
        'site_base_url': settings.SITE_BASE_URL,
    })

    text_content = render_to_string('newsletter/emails/issue.txt', {
        'issue': issue,
        'issue_apps': issue_apps,
        'subscriber': subscriber,
        'unsubscribe_url': f"{settings.SITE_BASE_URL}{subscriber.get_unsubscribe_url()}",
        'site_name': settings.SITE_NAME,
        'site_base_url': settings.SITE_BASE_URL,
    })

    # Send email
    send_mail(
        subject=issue.subject_line,
        message=text_content,
        html_message=html_content,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[subscriber.email],
        fail_silently=False,
    )