"""Django project package for LLM App Market."""

from .celery import app as celery_app

__all__ = ("celery_app",)
