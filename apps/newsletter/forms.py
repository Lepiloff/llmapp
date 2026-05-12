"""Newsletter forms."""
from __future__ import annotations

from django import forms

from .models import Subscriber


class SubscriberForm(forms.ModelForm):
    """Newsletter subscription form."""

    class Meta:
        model = Subscriber
        fields = ['email', 'frequency']
        widgets = {
            'email': forms.EmailInput(attrs={
                'placeholder': 'your@email.com',
                'class': 'form-control',
            }),
            'frequency': forms.Select(attrs={
                'class': 'form-control',
            }),
        }

    def clean_email(self):
        email = self.cleaned_data['email'].lower().strip()
        return email