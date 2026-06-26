"""Newsletter views for subscription management and issue display."""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .forms import SubscriberForm
from .models import EmailClick, EmailOpen, Issue, Subscriber
from .utils import get_client_ip


@require_http_methods(["GET", "POST"])
def subscribe(request: HttpRequest) -> HttpResponse:
    """Newsletter subscription page."""
    if request.method == "POST":
        form = SubscriberForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            source = request.GET.get('source', 'website')

            # Check if already subscribed
            try:
                subscriber = Subscriber.objects.get(email=email)
                if subscriber.is_active:
                    messages.info(request, "You're already subscribed to our newsletter!")
                    return redirect('newsletter:subscribe_success')
                elif subscriber.status == Subscriber.Status.PENDING:
                    # Resend confirmation
                    send_confirmation_email(subscriber)
                    messages.info(request, "We've resent the confirmation email. Please check your inbox.")
                    return redirect('newsletter:subscribe_success')
                else:
                    # Reactivate
                    subscriber.status = Subscriber.Status.PENDING
                    subscriber.source = source
                    subscriber.ip_address = get_client_ip(request)
                    subscriber.save(update_fields=['status', 'source', 'ip_address'])
                    send_confirmation_email(subscriber)
            except Subscriber.DoesNotExist:
                # Create new subscriber
                subscriber = form.save(commit=False)
                subscriber.source = source
                subscriber.ip_address = get_client_ip(request)
                subscriber.user_agent = request.META.get('HTTP_USER_AGENT', '')[:1000]
                subscriber.save()
                send_confirmation_email(subscriber)

            messages.success(request, "Thanks for subscribing! Please check your email to confirm your subscription.")
            return redirect('newsletter:subscribe_success')
    else:
        form = SubscriberForm()

    context = {
        'form': form,
        'source': request.GET.get('source', ''),
    }

    return render(request, 'newsletter/subscribe.html', context)


def subscribe_success(request: HttpRequest) -> HttpResponse:
    """Subscription success page."""
    return render(request, 'newsletter/subscribe_success.html')


def confirm_subscription(request: HttpRequest, token: str) -> HttpResponse:
    """Email confirmation endpoint."""
    try:
        subscriber = Subscriber.objects.get(confirmation_token=token)

        if subscriber.status == Subscriber.Status.PENDING:
            subscriber.confirm_subscription()
            messages.success(request, "Your subscription has been confirmed! Welcome to our newsletter.")
        else:
            messages.info(request, "Your subscription was already confirmed.")

        return render(request, 'newsletter/confirm_success.html', {'subscriber': subscriber})

    except Subscriber.DoesNotExist as exc:
        raise Http404("Invalid confirmation link") from exc


def unsubscribe(request: HttpRequest, token: str) -> HttpResponse:
    """Unsubscribe endpoint."""
    try:
        subscriber = Subscriber.objects.get(confirmation_token=token)

        if request.method == "POST":
            subscriber.unsubscribe()
            messages.success(request, "You have been unsubscribed from our newsletter.")
            return render(request, 'newsletter/unsubscribe_success.html')

        return render(request, 'newsletter/unsubscribe_confirm.html', {'subscriber': subscriber})

    except Subscriber.DoesNotExist as exc:
        raise Http404("Invalid unsubscribe link") from exc


def issue_archive(request: HttpRequest) -> HttpResponse:
    """Newsletter archive page."""
    issues = Issue.objects.filter(status=Issue.Status.SENT).order_by('-sent_at')

    context = {
        'issues': issues,
    }

    return render(request, 'newsletter/issue_archive.html', context)


def issue_detail(request: HttpRequest, slug: str) -> HttpResponse:
    """Individual newsletter issue page."""
    issue = get_object_or_404(
        Issue.objects.filter(status=Issue.Status.SENT).prefetch_related(
            'issueapp_set__app__platforms',
            'issueapp_set__app__categories'
        ),
        slug=slug
    )

    # Get featured apps in order
    issue_apps = issue.issueapp_set.all().order_by('sort_order')

    context = {
        'issue': issue,
        'issue_apps': issue_apps,
    }

    return render(request, 'newsletter/issue_detail.html', context)


def track_email_open(request: HttpRequest, issue_id: int, subscriber_id: int) -> HttpResponse:
    """Track email opens via tracking pixel."""
    try:
        issue = Issue.objects.get(id=issue_id)
        subscriber = Subscriber.objects.get(id=subscriber_id)

        # Create open record (unique constraint prevents duplicates)
        EmailOpen.objects.get_or_create(
            issue=issue,
            subscriber=subscriber,
            defaults={
                'ip_address': get_client_ip(request),
                'user_agent': request.META.get('HTTP_USER_AGENT', '')[:1000],
            }
        )

        # Update subscriber last opened
        subscriber.last_opened_at = timezone.now()
        subscriber.total_opens += 1
        subscriber.save(update_fields=['last_opened_at', 'total_opens'])

    except (Issue.DoesNotExist, Subscriber.DoesNotExist):
        pass  # Fail silently for tracking pixels

    # Return a 1x1 transparent pixel
    from django.http import HttpResponse
    pixel_data = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xdd\x8d\xb4\x1c\x00\x00\x00\x00IEND\xaeB`\x82'
    return HttpResponse(pixel_data, content_type='image/png')


def track_email_click(request: HttpRequest, issue_id: int, subscriber_id: int) -> HttpResponse:
    """Track email clicks and redirect to target URL."""
    target_url = request.GET.get('url', '')
    link_type = request.GET.get('type', 'website')

    if not target_url:
        raise Http404("Target URL is required")

    try:
        issue = Issue.objects.get(id=issue_id)
        subscriber = Subscriber.objects.get(id=subscriber_id)

        # Track the click
        EmailClick.objects.create(
            issue=issue,
            subscriber=subscriber,
            link_type=link_type,
            target_url=target_url,
            ip_address=get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:1000],
        )

        # Update subscriber click count
        subscriber.total_clicks += 1
        subscriber.save(update_fields=['total_clicks'])

    except (Issue.DoesNotExist, Subscriber.DoesNotExist):
        pass  # Fail silently but still redirect

    # Redirect to target URL
    return redirect(target_url)


def send_confirmation_email(subscriber: Subscriber) -> None:
    """Send confirmation email to new subscriber."""
    confirmation_url = f"{settings.SITE_BASE_URL}{subscriber.get_confirmation_url()}"

    subject = "Confirm your subscription to LLM App Market Newsletter"
    message = render_to_string('newsletter/emails/confirmation.txt', {
        'subscriber': subscriber,
        'confirmation_url': confirmation_url,
        'site_name': settings.SITE_NAME,
    })

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[subscriber.email],
            fail_silently=False,
        )
    except Exception as e:
        # Log error but don't fail the subscription
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send confirmation email to {subscriber.email}: {e}")
