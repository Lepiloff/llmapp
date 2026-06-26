"""Views for user submissions and claim requests."""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit

from apps.catalog.models import App

from .forms import ClaimRequestForm, SubmissionForm


def get_client_ip(request: HttpRequest) -> str:
    """Extract client IP address from request."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


@require_http_methods(["GET", "POST"])
@ratelimit(key='ip', rate='5/h', method='POST', block=True)
def submit_app(request: HttpRequest) -> HttpResponse:
    """Submit a new app to the catalog."""
    if request.method == "POST":
        form = SubmissionForm(request.POST)
        if form.is_valid():
            submission = form.save(commit=False)
            submission.submitter_ip = get_client_ip(request)
            submission.turnstile_token = form.cleaned_data.get('turnstile_response', '')
            submission.save()

            # Send notification email to editors
            try:
                send_notification_email(
                    subject=f"New app submission: {submission.app_name}",
                    template="emails/new_submission.txt",
                    context={'submission': submission},
                    recipient_list=settings.SUBMISSIONS_NOTIFY_EMAILS,
                )
            except Exception:
                # Don't fail the submission if email fails
                pass

            messages.success(
                request,
                "Thank you! Your app submission has been received and will be reviewed shortly."
            )
            return redirect('submissions:submit_success')
    else:
        form = SubmissionForm()

    return render(request, 'submissions/submit_app.html', {'form': form})


def submit_success(request: HttpRequest) -> HttpResponse:
    """Success page after app submission."""
    return render(request, 'submissions/submit_success.html')


@require_http_methods(["GET", "POST"])
@ratelimit(key='ip', rate='3/h', method='POST', block=True)
def claim_app(request: HttpRequest, slug: str) -> HttpResponse:
    """Claim ownership of an existing app."""
    app = get_object_or_404(App.published.all(), slug=slug)

    # Check if app is already claimed
    if app.developer_claim_status == "claimed":
        return render(request, 'submissions/already_claimed.html', {'app': app})

    # Check for pending claim
    pending_claim = app.claim_requests.filter(status="pending").first()
    if pending_claim:
        return render(request, 'submissions/claim_pending.html', {
            'app': app,
            'pending_claim': pending_claim
        })

    if request.method == "POST":
        form = ClaimRequestForm(request.POST)
        if form.is_valid():
            claim = form.save(commit=False)
            claim.app = app
            claim.submitter_ip = get_client_ip(request)
            claim.turnstile_token = form.cleaned_data.get('turnstile_response', '')
            claim.save()

            # Update app claim status to pending
            app.developer_claim_status = "pending"
            app.save(update_fields=['developer_claim_status'])

            # Send notification email to editors
            try:
                send_notification_email(
                    subject=f"New app claim request: {app.name}",
                    template="emails/new_claim_request.txt",
                    context={'claim': claim, 'app': app},
                    recipient_list=settings.SUBMISSIONS_NOTIFY_EMAILS,
                )
            except Exception:
                # Don't fail the submission if email fails
                pass

            messages.success(
                request,
                f"Your claim request for '{app.name}' has been submitted and will be reviewed."
            )
            return redirect('submissions:claim_success', slug=slug)
    else:
        form = ClaimRequestForm()

    return render(request, 'submissions/claim_app.html', {
        'form': form,
        'app': app
    })


def claim_success(request: HttpRequest, slug: str) -> HttpResponse:
    """Success page after claim submission."""
    app = get_object_or_404(App.published.all(), slug=slug)
    return render(request, 'submissions/claim_success.html', {'app': app})


def send_notification_email(subject: str, template: str, context: dict, recipient_list: list[str]) -> None:
    """Send notification email to editors."""
    message = render_to_string(template, context)
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=recipient_list,
        fail_silently=False,
    )