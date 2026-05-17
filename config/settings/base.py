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
    "apps.agent",
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
AGENT_REVIEW_DIGEST_EMAILS = config(
    "AGENT_REVIEW_DIGEST_EMAILS", default="", cast=Csv()
) or []

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
    # ---- Ingest ----
    "ingest_mcp_registry": {
        "task": "apps.sources.tasks.ingest_mcp_registry",
        "schedule": crontab(hour=4, minute=0),
    },
    "check_app_links_batch": {
        "task": "apps.sources.tasks.check_app_links_batch",
        "schedule": crontab(hour=5, minute=0),
    },
    # ---- Search / sitemap ----
    "refresh_search_vectors_batch": {
        "task": "apps.search.tasks.refresh_search_vectors_batch",
        "schedule": crontab(hour=3, minute=0),
    },
    "update_popular_searches": {
        "task": "apps.search.tasks.update_popular_searches",
        "schedule": crontab(hour=3, minute=30),
    },
    "rebuild_sitemap": {
        "task": "apps.seo.tasks.rebuild_sitemap",
        "schedule": crontab(minute="*/30"),
    },
    "ping_search_engines": {
        "task": "apps.seo.tasks.ping_search_engines",
        "schedule": crontab(hour=8, minute=0),
    },
    "generate_seo_reports": {
        "task": "apps.seo.tasks.generate_seo_reports",
        "schedule": crontab(day_of_week="mon", hour=8, minute=15),
    },
    # ---- Analytics ----
    "calculate_trending_scores": {
        "task": "apps.analytics.tasks.calculate_trending_scores",
        "schedule": crontab(hour=2, minute=30),
    },
    "cleanup_old_analytics_data": {
        "task": "apps.analytics.tasks.cleanup_old_analytics_data",
        "schedule": crontab(day_of_week="sun", hour=4, minute=30),
    },
    # ---- Catalog ----
    "recalc_quality_scores_batch": {
        "task": "apps.catalog.tasks.recalc_quality_scores_batch",
        "schedule": crontab(hour=6, minute=0),
    },
    # ---- Newsletter ----
    "newsletter_draft": {
        "task": "apps.newsletter.tasks.create_weekly_draft",
        "schedule": crontab(day_of_week="fri", hour=6, minute=0),
    },
    # ---- Agent ----
    "agent_review_queue_digest": {
        "task": "apps.agent.tasks.send_review_queue_digest",
        "schedule": crontab(hour=7, minute=30),
    },
    "agent_discover_rss": {
        "task": "apps.agent.tasks.discover_rss",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    "agent_discover_github_mcp": {
        "task": "apps.agent.tasks.discover_github_mcp",
        "schedule": crontab(day_of_week="mon,wed,fri", hour=6, minute=30),
    },
    "agent_reactualize_apps_batch": {
        "task": "apps.agent.tasks.reactualize_apps_batch",
        "schedule": crontab(hour=7, minute=0),
    },
    "agent_budget_check": {
        "task": "apps.agent.tasks.agent_budget_check",
        "schedule": crontab(minute=15),  # hourly at :15
    },
    # ---- Retention ----
    "cleanup_old_agent_logs": {
        "task": "apps.agent.tasks.cleanup_old_agent_logs",
        "schedule": crontab(day_of_week="sun", hour=4, minute=0),
    },
    "cleanup_old_link_check_results": {
        "task": "apps.sources.tasks.cleanup_old_link_check_results",
        "schedule": crontab(day_of_week="sun", hour=4, minute=15),
    },
    "cleanup_old_search_logs": {
        "task": "apps.search.tasks.cleanup_old_search_logs",
        "schedule": crontab(day_of_week="sun", hour=4, minute=45),
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
# Agent pipeline (Phase 1+) — see docs/agent-pipeline.md
# ---------------------------------------------------------------------------
# Provider selection. The pipeline is dual-provider-capable; either Anthropic
# or OpenAI can be plugged in for the "primary" (full enrichment) and "cheap"
# (discovery / classification) roles, and the two roles can share a provider.
# A real key is only required when the matching provider is actually used.
# Both default empty so the agent app imports cleanly in CI / on dev machines
# without API access; the LLMProvider factory raises ImproperlyConfigured at
# call-time if a real provider is requested without credentials.
AGENT_LLM_PROVIDER_PRIMARY = config("AGENT_LLM_PROVIDER_PRIMARY", default="mock")
AGENT_LLM_PROVIDER_CHEAP = config("AGENT_LLM_PROVIDER_CHEAP", default="mock")
AGENT_LLM_MODEL_PRIMARY = config("AGENT_LLM_MODEL_PRIMARY", default="")
AGENT_LLM_MODEL_CHEAP = config("AGENT_LLM_MODEL_CHEAP", default="")
ANTHROPIC_API_KEY = config("ANTHROPIC_API_KEY", default="")
OPENAI_API_KEY = config("OPENAI_API_KEY", default="")
# Per-role pricing knobs. Primary (full enrichment, e.g. gpt-5.4-mini) and
# cheap (discovery classification, e.g. gpt-5.4-nano) use different models
# at different price points, so a single global pair would mis-cost one of
# them. Cached-input has its own line item: OpenAI bills tokens served from
# prompt cache at ~10% of the standard input rate, and `_estimate_cost_usd`
# subtracts cached from billable input before applying the cached price.
# All default to 0.0 so the agent app boots without API access; operators
# set real numbers before enabling discovery beat.
AGENT_OPENAI_PRIMARY_INPUT_COST_PER_1M_TOKENS = config(
    "AGENT_OPENAI_PRIMARY_INPUT_COST_PER_1M_TOKENS", default=0.0, cast=float
)
AGENT_OPENAI_PRIMARY_CACHED_COST_PER_1M_TOKENS = config(
    "AGENT_OPENAI_PRIMARY_CACHED_COST_PER_1M_TOKENS", default=0.0, cast=float
)
AGENT_OPENAI_PRIMARY_OUTPUT_COST_PER_1M_TOKENS = config(
    "AGENT_OPENAI_PRIMARY_OUTPUT_COST_PER_1M_TOKENS", default=0.0, cast=float
)
AGENT_OPENAI_CHEAP_INPUT_COST_PER_1M_TOKENS = config(
    "AGENT_OPENAI_CHEAP_INPUT_COST_PER_1M_TOKENS", default=0.0, cast=float
)
AGENT_OPENAI_CHEAP_CACHED_COST_PER_1M_TOKENS = config(
    "AGENT_OPENAI_CHEAP_CACHED_COST_PER_1M_TOKENS", default=0.0, cast=float
)
AGENT_OPENAI_CHEAP_OUTPUT_COST_PER_1M_TOKENS = config(
    "AGENT_OPENAI_CHEAP_OUTPUT_COST_PER_1M_TOKENS", default=0.0, cast=float
)

# Budget cap (hard stop at 100%, alert at 80%). Empty = no budget enforcement
# (acceptable only during Phase 1 mock-only mode); must be set before any
# real-API beat task is enabled.
AGENT_MONTHLY_BUDGET_USD = config("AGENT_MONTHLY_BUDGET_USD", default="", cast=str)

# Optional dedicated recipients for the Phase 5 budget alert at 80% / 100%.
# Falls back to AGENT_REVIEW_DIGEST_EMAILS → SUBMISSIONS_NOTIFY_EMAILS.
AGENT_BUDGET_ALERT_EMAILS = config(
    "AGENT_BUDGET_ALERT_EMAILS", default="", cast=Csv()
) or []

# Polite-client controls.
AGENT_RATE_LIMIT_RPS_PER_DOMAIN = config(
    "AGENT_RATE_LIMIT_RPS_PER_DOMAIN", default=1.0, cast=float
)

# Feature flag — controls which sources beat is allowed to run. Empty = all
# disabled; the manual `manage.py agent_run` command bypasses this so Phase 1
# can be exercised entirely from the CLI before beat is touched.
AGENT_SOURCES_ENABLED = config(
    "AGENT_SOURCES_ENABLED", default="", cast=Csv()
) or []
GITHUB_TOKEN = config("GITHUB_TOKEN", default="")

# Phase 4 re-actualization cadence. Apps whose freshest re-actualizable
# Source is older than this many days (or never enriched) are eligible
# for the daily reactualize beat. AGENT_REACTUALIZATION_ENABLED gates
# the beat itself — set to True before enabling the schedule entry.
AGENT_REACTUALIZATION_ENABLED = config(
    "AGENT_REACTUALIZATION_ENABLED", default=False, cast=bool
)
AGENT_REACTUALIZATION_INTERVAL_DAYS = config(
    "AGENT_REACTUALIZATION_INTERVAL_DAYS", default=30, cast=int
)
AGENT_REACTUALIZATION_BATCH_SIZE = config(
    "AGENT_REACTUALIZATION_BATCH_SIZE", default=20, cast=int
)

# SLA window for the editor review queue. Pending NeedsReviewQueueEntry
# rows older than this many days are flagged "overdue" on the SLA
# dashboard at /admin/agent/needsreviewqueueentry/sla-dashboard/.
AGENT_REVIEW_QUEUE_SLA_DAYS = config(
    "AGENT_REVIEW_QUEUE_SLA_DAYS", default=14, cast=int
)

# Retention windows for unbounded audit-trail tables. Each cleanup task is
# scheduled in CELERY_BEAT_SCHEDULE; tweaking the env var changes the
# cutoff without a code redeploy.
AGENT_LOG_RETENTION_DAYS = config(
    "AGENT_LOG_RETENTION_DAYS", default=180, cast=int
)
SOURCES_LINK_CHECK_RETENTION_DAYS = config(
    "SOURCES_LINK_CHECK_RETENTION_DAYS", default=30, cast=int
)
SEARCH_LOG_RETENTION_DAYS = config(
    "SEARCH_LOG_RETENTION_DAYS", default=90, cast=int
)

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
os.makedirs(MEDIA_ROOT, exist_ok=True)
