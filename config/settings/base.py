"""Base Django settings shared by every environment.

Architecture references:
  * docs/architecture.md § 1 (stack)
  * docs/architecture.md § 12 (background tasks)
  * docs/architecture.md § 13 (caching)
  * docs/architecture.md § 15 (observability)
"""
from __future__ import annotations

import os
from pathlib import Path

from celery.schedules import crontab
from decouple import Csv, config

BASE_DIR: Path = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY: str = config("SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG: bool = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS: list[str] = config(
    "ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv()
)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.contrib.sitemaps",
    "django.contrib.redirects",
    "django.contrib.postgres",
]

THIRD_PARTY_APPS = [
    "corsheaders",
    "django_celery_beat",
]

LOCAL_APPS = [
    "apps.core",
    "apps.catalog",
    "apps.sources",
    "apps.submissions",
    "apps.search",
    "apps.seo",
    "apps.editorial",
    "apps.analytics",
    "apps.newsletter",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

SITE_ID = 1

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.redirects.middleware.RedirectFallbackMiddleware",
    "apps.core.middleware.RequestIDMiddleware",
    "apps.core.middleware.HtmxAwareMiddleware",
]

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site_meta",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database (PostgreSQL is mandatory — pg_trgm + tsvector are core).
# ---------------------------------------------------------------------------
import dj_database_url

DATABASES = {
    "default": dj_database_url.config(
        default=config(
            "DATABASE_URL",
            default="postgres://llmmarket:llmmarket@localhost:5432/llmmarket",
        ),
        conn_max_age=60,
    )
}
DATABASES["default"].setdefault("ENGINE", "django.db.backends.postgresql")

# ---------------------------------------------------------------------------
# Cache (Redis-backed; fragment / page caches rely on `delete_pattern`).
# ---------------------------------------------------------------------------
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": True,
        },
        "KEY_PREFIX": "llmmarket",
    }
}
DJANGO_REDIS_IGNORE_EXCEPTIONS = True

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# I18N / TZ
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ---------------------------------------------------------------------------
# CORS — explicit allowlist, never *.
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS", default="", cast=Csv()
) or []
CORS_ALLOW_CREDENTIALS = False
CORS_ALLOW_ALL_ORIGINS = False  # prod must remain False
CORS_ALLOWED_METHODS = ["GET", "POST", "HEAD", "OPTIONS"]

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL_BACKEND = config(
    "EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="hello@llmappmarket.com")
SUBMISSIONS_NOTIFY_EMAILS = config(
    "SUBMISSIONS_NOTIFY_EMAILS", default="editor@llmappmarket.com", cast=Csv()
)

# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------
SITE_NAME = "LLM App Market"
SITE_TAGLINE = "Discover apps, connectors and agents for ChatGPT, Claude, Gemini and beyond."
SITE_BASE_URL = config("SITE_BASE_URL", default="https://llmappmarket.com")

# ---------------------------------------------------------------------------
# Captcha (Cloudflare Turnstile)
# ---------------------------------------------------------------------------
TURNSTILE_SITE_KEY = config("TURNSTILE_SITE_KEY", default="")
TURNSTILE_SECRET_KEY = config("TURNSTILE_SECRET_KEY", default="")

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=REDIS_URL)
CELERY_TASK_ALWAYS_EAGER = config("CELERY_TASK_ALWAYS_EAGER", default=False, cast=bool)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

CELERY_BEAT_SCHEDULE = {
    "ingest_mcp_registry": {
        "task": "apps.sources.tasks.ingest_mcp_registry",
        "schedule": crontab(hour=4, minute=0),
    },
    "check_app_links_batch": {
        "task": "apps.sources.tasks.check_app_links_batch",
        "schedule": crontab(hour=5, minute=0),
    },
    "rebuild_sitemap": {
        "task": "apps.seo.tasks.rebuild_sitemap",
        "schedule": crontab(minute="*/30"),
    },
    "refresh_search_vectors_batch": {
        "task": "apps.search.tasks.refresh_search_vectors_batch",
        "schedule": crontab(hour=3, minute=0),
    },
    "newsletter_draft": {
        "task": "apps.newsletter.tasks.create_weekly_draft",
        "schedule": crontab(day_of_week="fri", hour=6, minute=0),
    },
}

# ---------------------------------------------------------------------------
# Source / ingest
# ---------------------------------------------------------------------------
MCP_REGISTRY_BASE_URL = config(
    "MCP_REGISTRY_BASE_URL", default="https://registry.modelcontextprotocol.io/v1"
)

# ---------------------------------------------------------------------------
# Catalog tunables
# ---------------------------------------------------------------------------
CATALOG_PAGE_SIZE = 24
CATALOG_TRIGRAM_THRESHOLD = 0.25
TRENDING_WINDOW_DAYS = 7
QUALITY_SCORE_HIDDEN_THRESHOLD = 30  # cards below this are excluded from home defaults

# ---------------------------------------------------------------------------
# Logging — structured JSON
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(name)s %(levelname)s %(message)s",
        },
        "plain": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "celery": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# ---------------------------------------------------------------------------
# Sentry
# ---------------------------------------------------------------------------
SENTRY_DSN = config("SENTRY_DSN", default="")
if SENTRY_DSN:  # pragma: no cover - external integration
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        traces_sample_rate=0.05,
        send_default_pii=False,
    )

# ---------------------------------------------------------------------------
# Security defaults — production settings override stricter values.
# ---------------------------------------------------------------------------
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
os.makedirs(MEDIA_ROOT, exist_ok=True)
