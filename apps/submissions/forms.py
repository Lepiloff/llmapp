"""Forms for user submissions and claim requests."""
from __future__ import annotations

import requests
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError

from .models import ClaimRequest, Submission


class TurnstileWidget(forms.Widget):
    """Cloudflare Turnstile CAPTCHA widget."""

    def render(self, name, value, attrs=None, renderer=None):
        site_key = getattr(settings, 'TURNSTILE_SITE_KEY', '')
        if not site_key:
            return ''

        return f'''
        <div class="cf-turnstile" data-sitekey="{site_key}"></div>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
        '''


class SubmissionForm(forms.ModelForm):
    """Form for submitting new apps to the catalog."""

    turnstile_response = forms.CharField(
        widget=TurnstileWidget(),
        required=True,
        help_text="Please complete the CAPTCHA verification"
    )

    class Meta:
        model = Submission
        fields = [
            'app_name',
            'short_description',
            'long_description',
            'developer_name',
            'developer_email',
            'official_url',
            'install_url',
            'repo_url',
            'suggested_platforms',
            'suggested_categories',
        ]
        widgets = {
            'app_name': forms.TextInput(attrs={
                'placeholder': 'e.g., ChatGPT Code Interpreter',
                'class': 'form-control'
            }),
            'short_description': forms.Textarea(attrs={
                'placeholder': 'Brief description of what this app does (max 280 chars)',
                'rows': 3,
                'class': 'form-control',
                'maxlength': 280
            }),
            'long_description': forms.Textarea(attrs={
                'placeholder': 'Detailed description (optional)',
                'rows': 5,
                'class': 'form-control'
            }),
            'developer_name': forms.TextInput(attrs={
                'placeholder': 'Company or developer name',
                'class': 'form-control'
            }),
            'developer_email': forms.EmailInput(attrs={
                'placeholder': 'your@email.com',
                'class': 'form-control'
            }),
            'official_url': forms.URLInput(attrs={
                'placeholder': 'https://example.com',
                'class': 'form-control'
            }),
            'install_url': forms.URLInput(attrs={
                'placeholder': 'https://example.com/install (optional)',
                'class': 'form-control'
            }),
            'repo_url': forms.URLInput(attrs={
                'placeholder': 'https://github.com/user/repo (optional)',
                'class': 'form-control'
            }),
            'suggested_platforms': forms.TextInput(attrs={
                'placeholder': 'ChatGPT, Claude, Gemini...',
                'class': 'form-control'
            }),
            'suggested_categories': forms.TextInput(attrs={
                'placeholder': 'Productivity, Developer Tools...',
                'class': 'form-control'
            }),
        }

    def clean_turnstile_response(self):
        """Verify Turnstile CAPTCHA response."""
        token = self.cleaned_data.get('turnstile_response')
        if not token:
            raise ValidationError("CAPTCHA verification is required")

        secret_key = getattr(settings, 'TURNSTILE_SECRET_KEY', '')
        if not secret_key:
            # Skip verification in development if no secret key
            return token

        # Verify with Cloudflare
        try:
            response = requests.post(
                'https://challenges.cloudflare.com/turnstile/v0/siteverify',
                data={
                    'secret': secret_key,
                    'response': token,
                },
                timeout=10
            )
            result = response.json()
            if not result.get('success'):
                raise ValidationError("CAPTCHA verification failed")
        except Exception as exc:
            raise ValidationError("CAPTCHA verification failed") from exc

        return token

    def clean_short_description(self):
        """Validate short description length."""
        description = self.cleaned_data.get('short_description', '')
        if len(description) > 280:
            raise ValidationError("Description must be 280 characters or less")
        return description


class ClaimRequestForm(forms.ModelForm):
    """Form for claiming ownership of an existing app."""

    turnstile_response = forms.CharField(
        widget=TurnstileWidget(),
        required=True,
        help_text="Please complete the CAPTCHA verification"
    )

    class Meta:
        model = ClaimRequest
        fields = [
            'claimant_name',
            'claimant_email',
            'verification_method',
            'evidence',
        ]
        widgets = {
            'claimant_name': forms.TextInput(attrs={
                'placeholder': 'Your name',
                'class': 'form-control'
            }),
            'claimant_email': forms.EmailInput(attrs={
                'placeholder': 'your@email.com',
                'class': 'form-control'
            }),
            'verification_method': forms.TextInput(attrs={
                'placeholder': 'e.g., GitHub repository access, email from official domain',
                'class': 'form-control'
            }),
            'evidence': forms.Textarea(attrs={
                'placeholder': 'Provide evidence that you own this app (links, screenshots, etc.)',
                'rows': 5,
                'class': 'form-control'
            }),
        }

    def clean_turnstile_response(self):
        """Verify Turnstile CAPTCHA response."""
        return SubmissionForm.clean_turnstile_response(self)
