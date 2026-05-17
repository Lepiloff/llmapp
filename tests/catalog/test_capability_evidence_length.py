"""Regression: AppCapability.note now stores up to 500 characters.

Previously 200, which routinely truncated mid-citation when the LLM
quoted a full README sentence.
"""
from __future__ import annotations

import pytest

from apps.catalog.models import App, AppCapability, Capability


@pytest.mark.django_db
def test_note_field_accepts_500_characters() -> None:
    app = App.objects.create(name="Acme", slug="acme-cap", short_description="x")
    cap = Capability.objects.create(key="read_data", label="Reads data")
    long_quote = "x" * 500

    row = AppCapability.objects.create(
        app=app,
        capability=cap,
        value=AppCapability.CapabilityValue.YES,
        note=long_quote,
    )
    row.refresh_from_db()
    assert row.note == long_quote
    assert len(row.note) == 500


@pytest.mark.django_db
def test_note_field_max_length_is_500() -> None:
    from apps.catalog.models import AppCapability as M

    assert M._meta.get_field("note").max_length == 500
