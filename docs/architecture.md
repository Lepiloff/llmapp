# LLM App Market — backend architecture

> Technical companion to [`business.md`](./business.md).
> This document describes **how to build** the MVP described there:
> stack, project layout, full data model, search layer, ingest pipelines, HTMX patterns, admin moderation, SEO infrastructure, background tasks, deployment topology.
>
> Target reader: a backend engineer (or LLM agent) starting from an empty repository who wants to begin implementation without re-asking architectural questions.

---

## Table of contents

1. [Tech stack](#1-tech-stack)
2. [High-level architecture](#2-high-level-architecture)
3. [Project layout](#3-project-layout)
4. [Data model](#4-data-model)
5. [Key Django model code](#5-key-django-model-code)
6. [Search layer (PostgreSQL)](#6-search-layer-postgresql)
7. [HTMX patterns](#7-htmx-patterns)
8. [Routing](#8-routing)
9. [Sources & ingest](#9-sources--ingest)
10. [Submissions & Claims (no accounts)](#10-submissions--claims-no-accounts)
11. [SEO infrastructure](#11-seo-infrastructure)
12. [Background tasks](#12-background-tasks)
13. [Caching](#13-caching)
14. [Admin](#14-admin)
15. [Observability & ops](#15-observability--ops)
16. [Local dev & deploy](#16-local-dev--deploy)

---

## 1. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Modern stable, good async support, ecosystem |
| Web framework | Django 5.x | Batteries-included, strong ORM, admin, sitemaps, forms, security |
| DB + search | PostgreSQL 16 | Source of truth **and** the search engine for MVP: `tsvector` full-text search, `pg_trgm` for typo tolerance, ORM-level facets |
| Queue | Celery 5 | Standard for Django background work |
| Broker / cache | Redis 7 | Celery broker + Django cache backend |
| Frontend | Django templates + htmx + Alpine.js + Tailwind | SSR-first, SEO-friendly, minimal JS |
| Object storage | S3-compatible (Cloudflare R2 / AWS S3) | Logos, screenshots, OG images |
| Email | Transactional via Postmark / Resend / SES | Submission/claim confirmations + digest |
| Errors | Sentry | Standard |
| Captcha | Cloudflare Turnstile | Free, accessible, no Google dependency |
| Deploy | Docker + single VPS / Fly.io / Hetzner | One app, one worker, one beat |

No accounts, no JS framework, no separate frontend, **no dedicated search engine**. The bet is: **SSR HTML pages indexable by search engines, with progressive HTMX enhancements, backed by a single Postgres database** that handles relational data, full-text search and facet aggregation.

A separate search engine (Typesense / Meilisearch / Elastic) is explicitly out of scope for the MVP. Postgres comfortably handles tens of thousands of catalog rows with sub-100ms FTS + facet queries when the indexes described in § 4.4 and § 6 are in place. The architecture is shaped so that swapping in a dedicated engine later (when the catalog grows or query patterns diverge) is a one-app change in `apps/search/` — no schema migration in `catalog/`.

---

## 2. High-level architecture

```
                            ┌────────────────────────┐
                            │       Browser          │
                            │ (HTMX + Tailwind UI)   │
                            └──────────┬─────────────┘
                                       │ HTTPS (HTML + partials)
                                       ▼
              ┌───────────────────────────────────────────────┐
              │                Django app                     │
              │   ┌────────────┐   ┌────────────────────────┐ │
              │   │ Views      │──▶│ Services / domain layer│ │
              │   │ (HTMX)     │   └──────────┬─────────────┘ │
              │   │ Forms      │              │               │
              │   │ Sitemaps   │   ┌──────────▼─────────────┐ │
              │   │ Admin      │   │ Django ORM             │ │
              │   └────────────┘   └──────────┬─────────────┘ │
              └────────────────────┬──────────┴───────────────┘
                                   │                  ▲
                  enqueue tasks    │                  │ refresh search_vector
                                   ▼                  │ on save (signal)
                          ┌────────────────┐          │
                          │ Celery worker  │──────────┘
                          │ + Celery beat  │
                          └───┬──────┬─────┘
                              │      │
       ┌──────────────────────┘      │
       ▼                              ▼
┌──────────────┐   ┌──────────────┐   ┌─────────────────────────────────┐
│ MCP Registry │   │ Link checker │   │ PostgreSQL                      │
│   API        │   │ (HTTP HEAD)  │   │ • catalog tables                │
└──────────────┘   └──────────────┘   │ • App.search_vector (GIN)       │
                                      │ • pg_trgm indexes               │
                                      │ • facet aggregation via ORM     │
                                      └─────────────────────────────────┘
        │
        ▼
   Drafts → moderation queue → published `App`
```

Key invariants:

- **Postgres is the source of truth AND the search engine** in MVP. There is no second store.
- **`App.search_vector` is materialized** in a column and refreshed via signals on every save (and on related M2M changes). A nightly safety-net task recomputes vectors for the whole table.
- **Facets are computed at query time** via ORM aggregations. For MVP catalog volumes (300 – ~20K rows) this is < 100ms p95 with the indexes from § 4.4.
- **Public visibility is binary** (`App.status = published` or not). Featured/quality only affects ordering.
- **Submissions and claims never create published records directly.** They go through `pending` → editor → `approved`.
- **`apps/search/` is the only place that knows about full-text search.** If we later swap Postgres FTS for a dedicated engine, only `apps/search/` changes.

---

## 3. Project layout

```
llmmarket/
├── config/
│   ├── settings/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── dev.py
│   │   ├── prod.py
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
├── apps/
│   ├── catalog/
│   │   ├── models.py          # Platform, ListingType, App, AppPlatform,
│   │   │                      # Category, AppCategory, Capability, AppCapability,
│   │   │                      # UseCase, AppUseCase
│   │   ├── managers.py        # PublishedAppManager
│   │   ├── services.py        # recalc_quality_score, transition_to_published
│   │   ├── admin.py
│   │   ├── views.py           # catalog list, app detail, platform/category pages
│   │   ├── urls.py
│   │   ├── signals.py         # post_save / m2m_changed -> refresh_search_vector_task.delay()
│   │   ├── forms.py
│   │   └── templatetags/
│   ├── sources/
│   │   ├── models.py          # Source
│   │   ├── base.py            # BaseSource interface
│   │   ├── mcp_registry.py    # MCPRegistrySource
│   │   ├── tasks.py           # ingest_mcp_registry, check_app_links
│   │   └── admin.py
│   ├── submissions/
│   │   ├── models.py          # Submission, ClaimRequest
│   │   ├── forms.py
│   │   ├── views.py
│   │   ├── admin.py
│   │   ├── emails.py
│   │   └── urls.py
│   ├── search/
│   │   ├── vector.py          # search_vector expression, refresh_search_vector()
│   │   ├── facets.py          # facet aggregation helpers
│   │   ├── filters.py         # parse GET → ORM filters, sort options
│   │   ├── tasks.py           # refresh_search_vectors (batch / safety net)
│   │   └── views.py           # /apps/ HTMX search + facet endpoint
│   ├── seo/
│   │   ├── sitemaps.py
│   │   ├── structured_data.py # JSON-LD helpers
│   │   ├── robots.py
│   │   └── views.py
│   ├── editorial/
│   │   ├── models.py          # Post, Collection, Comparison
│   │   ├── views.py
│   │   ├── admin.py
│   │   └── urls.py
│   ├── analytics/
│   │   ├── models.py          # ClickEvent
│   │   ├── views.py           # outbound redirect
│   │   └── tasks.py
│   ├── newsletter/
│   │   ├── models.py          # Subscriber, Issue
│   │   ├── forms.py
│   │   ├── views.py
│   │   ├── tasks.py           # send_issue
│   │   └── admin.py
│   └── core/
│       ├── middleware.py      # request_id, htmx detection
│       ├── context_processors.py
│       └── healthcheck.py
├── templates/
│   ├── base.html
│   ├── partials/
│   │   ├── app_card.html
│   │   ├── facets.html
│   │   ├── pagination.html
│   ├── catalog/
│   │   ├── list.html
│   │   ├── detail.html
│   │   ├── platform.html
│   │   ├── category.html
│   ├── submissions/
│   ├── editorial/
│   └── newsletter/
├── static/
│   └── (tailwind output + a tiny app.js for htmx events)
├── docker/
│   ├── Dockerfile
│   ├── entrypoint.sh
├── docker-compose.yml
├── Makefile
├── pyproject.toml
└── manage.py
```

Each Django app is a vertical slice — models, views, urls, templates, tasks, admin in one place. No “shared utils” bucket; everything domain-specific belongs to its domain.

---

## 4. Data model

### 4.1. Entities

| Entity | Purpose |
|---|---|
| `Platform` | ChatGPT, Claude, Gemini, MCP, Enterprise |
| `ListingType` | App / Connector / Interactive App / MCP Server / Gemini App / Agent |
| `App` | Core listing |
| `AppPlatform` | M2M through table: `App ↔ Platform` with per-platform metadata |
| `Category` | Productivity, Developer Tools, etc. (10 in MVP) |
| `AppCategory` | M2M `App ↔ Category` |
| `Capability` | read_data, write_actions, interactive_ui, ... |
| `AppCapability` | M2M through with explicit value (`yes` / `no` / `unknown`) |
| `UseCase` | free-form short phrases ("turn notes into slides") |
| `AppUseCase` | M2M `App ↔ UseCase` |
| `Source` | Where a listing came from (manual, MCP registry, submission, ...) |
| `Submission` | Public submit form record |
| `ClaimRequest` | Public claim form record |
| `ClickEvent` | Outbound redirect click |
| `Subscriber` | Newsletter subscriber |
| `Issue` | Newsletter issue |
| `Post` | Editorial blog post |
| `Collection` | Editorial podborka |
| `Comparison` | Editorial comparison page |

### 4.2. ER diagram (ASCII)

```
   Platform                       ListingType
       ▲                              ▲
       │                              │
       │  AppPlatform                 │  App.listing_types (M2M)
       │  ─ compatibility_status      │
       │  ─ supported_plans (JSON)    │
       │  ─ region_availability       │
       │  ─ scope_summary             │
       │  ─ official_directory_url    │
       │  ─ metadata (JSON)           │
       │                              │
       └──────────┐         ┌─────────┘
                  ▼         ▼
                ┌───────────────┐
                │      App      │  ◀──── Source (FK on App, multiple per app)
                │ (slug unique) │
                └──┬──────────┬─┘
                   │          │
       AppCategory │          │ AppCapability
       ─ M2M       │          │ ─ value (yes/no/unknown)
                   ▼          ▼
              Category    Capability
                   ▲          ▲
                   │          │
                   │          │ AppUseCase (M2M App↔UseCase)
                   │          │
                ┌──┴──────────┴──┐
                │     UseCase    │
                └────────────────┘

   Submission ──▶ (editor converts) ──▶ App
   ClaimRequest ──▶ run_claim_auto_check (Celery, sets auto_check_status only)
              ──▶ (editor approves in admin) ──▶ App.developer_claim_status = claimed
   ClickEvent ──▶ App (FK)
   LinkCheckResult, LinkHealth ──▶ App (FK; drives auto-deprecate)
   Subscriber, Issue (newsletter app, no FK to App)
```

### 4.3. Status enums

All as Django `TextChoices`:

```python
# App.status is the EDITORIAL lifecycle / visibility flag.
# It is set by editors (or by the Published manager guard), never by the
# automated link-checker. The product's own state lives in `launch_status`.
class AppStatus(TextChoices):
    DRAFT = "draft"          # not visible publicly
    PUBLISHED = "published"  # visible on the site
    HIDDEN = "hidden"        # editor pulled it (spam, abuse, dead listing)


# `launch_status` reflects the PRODUCT's state as observed in the world.
# Updated by editors and by `check_single_app_link` (which can flip to
# DEPRECATED after 7 consecutive failures). Has no automatic effect on
# visibility — editors decide whether to hide a deprecated listing.


# Trust model — three independent axes (see business.md § 6.5).
# Never collapse into a single field.
class PlatformVerificationStatus(TextChoices):
    OFFICIAL = "official"      # listed in the platform's own directory
    NOT_LISTED = "not_listed"  # confirmed absent from any platform directory
    UNKNOWN = "unknown"


class EditorialReviewStatus(TextChoices):
    REVIEWED = "reviewed"
    UNREVIEWED = "unreviewed"


class DeveloperClaimStatus(TextChoices):
    UNCLAIMED = "unclaimed"
    PENDING = "pending"
    CLAIMED = "claimed"


class LaunchStatus(TextChoices):
    LIVE = "live"
    BETA = "beta"
    WAITLIST = "waitlist"
    DEPRECATED = "deprecated"


class PricingModel(TextChoices):
    FREE = "free"
    PAID = "paid"
    FREEMIUM = "freemium"
    UNKNOWN = "unknown"


class CompatibilityStatus(TextChoices):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    PREVIEW = "preview"
    UNKNOWN = "unknown"


class CapabilityValue(TextChoices):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class SubmissionStatus(TextChoices):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ClaimStatus(TextChoices):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class SourceType(TextChoices):
    MANUAL = "manual"
    MCP_REGISTRY = "mcp_registry"
    SUBMISSION = "submission"
    CHATGPT_DIRECTORY = "chatgpt_directory"
    CLAUDE_CONNECTORS = "claude_connectors"
```

### 4.4. Index strategy

| Index | Reason |
|---|---|
| `App.slug` unique | Lookup by slug on detail pages |
| `App(status, quality_score DESC)` | Default catalog ordering |
| `App(first_seen_at DESC)` | “Newly added” feed |
| `AppPlatform(platform_id, app_id)` | Filtered by platform |
| `AppCategory(category_id, app_id)` | Filtered by category |
| `ClickEvent(app_id, created_at)` | Trending calculation |
| **GIN on `App.search_vector`** | Full-text search (`@@`) — the primary search index |
| **GIN trigram on `App.name`** (`gin_trgm_ops`) | Typo-tolerant fuzzy match for the search box |
| **GIN trigram on `App.developer_name`** | Lookups like "by Anthropic", "by …" |

Required Postgres extensions (created in an early migration):

```python
from django.contrib.postgres.operations import TrigramExtension
from django.contrib.postgres.indexes import GinIndex

class Migration(migrations.Migration):
    operations = [
        TrigramExtension(),  # pg_trgm
        # btree_gin is optional but useful if you want to combine
        # `status` filter with trigram in a single index later.
    ]
```

`unaccent` extension is **not** added in MVP (English-first content; can be enabled later for non-Latin queries with one migration).

---

## 5. Key Django model code

The full set is too long to inline, so this section shows the **decisive** pieces. Anything trivial (`Subscriber`, `Post`) is left to a competent implementer.

### 5.1. `apps/catalog/models.py` — core

```python
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import TextChoices, Q
from django.utils.text import slugify
from django.utils import timezone


class Platform(models.Model):
    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)            # internal/canonical identifier ("claude")
    public_path = models.CharField(
        max_length=80, unique=True,
        help_text="URL segment used in platform pages and sitemap, "
                  "e.g. 'chatgpt-apps', 'claude-connectors', 'mcp-servers'.",
    )
    website_url = models.URLField(blank=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self) -> str:
        return f"/{self.public_path}/"


class ListingType(models.Model):
    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Category(models.Model):
    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    sort_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Capability(models.Model):
    key = models.SlugField(unique=True)        # "read_data", "write_actions", ...
    label = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    sort_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "key"]

    def __str__(self):
        return self.label


class UseCase(models.Model):
    title = models.CharField(max_length=160)
    slug = models.SlugField(unique=True, max_length=200)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)[:200]
        super().save(*args, **kwargs)


class PublishedAppManager(models.Manager):
    def get_queryset(self):
        return (
            super().get_queryset()
            .filter(status=App.AppStatus.PUBLISHED, is_indexable=True)
        )


class App(models.Model):
    class AppStatus(TextChoices):
        """Editorial lifecycle / public visibility. Set by editors only.

        Product-side state lives in `launch_status`. These two axes are
        deliberately separate: a deprecated product may still warrant a
        published card with a deprecation badge, and an editor may HIDE
        an actively-running product (spam, abuse).
        """
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        HIDDEN = "hidden", "Hidden by editor"

    # Trust model split into three independent axes (see business.md § 6.5).
    class PlatformVerificationStatus(TextChoices):
        OFFICIAL = "official", "Listed in official platform directory"
        NOT_LISTED = "not_listed", "Not listed in any platform directory"
        UNKNOWN = "unknown", "Unknown"

    class EditorialReviewStatus(TextChoices):
        REVIEWED = "reviewed", "Reviewed by editor"
        UNREVIEWED = "unreviewed", "Not yet reviewed"

    class DeveloperClaimStatus(TextChoices):
        UNCLAIMED = "unclaimed", "Unclaimed"
        PENDING = "pending", "Claim pending"
        CLAIMED = "claimed", "Verified by developer"

    class LaunchStatus(TextChoices):
        LIVE = "live", "Live"
        BETA = "beta", "Beta"
        WAITLIST = "waitlist", "Waitlist"
        DEPRECATED = "deprecated", "Deprecated"

    class PricingModel(TextChoices):
        FREE = "free", "Free"
        PAID = "paid", "Paid"
        FREEMIUM = "freemium", "Freemium"
        UNKNOWN = "unknown", "Unknown"

    # Identity
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, max_length=220)
    listing_types = models.ManyToManyField(ListingType, related_name="apps")

    # Descriptions
    short_description = models.CharField(max_length=280)
    long_description = models.TextField(blank=True)
    verdict = models.CharField(max_length=280, blank=True)

    # People / company
    developer_name = models.CharField(max_length=200, blank=True)
    developer_url = models.URLField(blank=True)
    contact_email = models.EmailField(blank=True)  # set by Claim verification

    # Media
    logo = models.ImageField(upload_to="apps/logos/", blank=True, null=True)
    cover_image = models.ImageField(upload_to="apps/covers/", blank=True, null=True)

    # Links
    official_page_url = models.URLField(blank=True)
    install_url = models.URLField(blank=True)
    repo_url = models.URLField(blank=True)

    # Classification
    platforms = models.ManyToManyField(Platform, through="AppPlatform", related_name="apps")
    categories = models.ManyToManyField(Category, through="AppCategory", related_name="apps")
    capabilities = models.ManyToManyField(Capability, through="AppCapability", related_name="apps")
    use_cases = models.ManyToManyField(UseCase, through="AppUseCase", related_name="apps")

    # Status
    status = models.CharField(max_length=20, choices=AppStatus.choices, default=AppStatus.DRAFT)

    # Three independent trust axes; never collapse into a single field.
    platform_verification_status = models.CharField(
        max_length=20, choices=PlatformVerificationStatus.choices,
        default=PlatformVerificationStatus.UNKNOWN,
    )
    editorial_review_status = models.CharField(
        max_length=20, choices=EditorialReviewStatus.choices,
        default=EditorialReviewStatus.UNREVIEWED,
    )
    developer_claim_status = models.CharField(
        max_length=20, choices=DeveloperClaimStatus.choices,
        default=DeveloperClaimStatus.UNCLAIMED,
    )

    launch_status = models.CharField(
        max_length=20, choices=LaunchStatus.choices, default=LaunchStatus.LIVE,
    )
    pricing_model = models.CharField(
        max_length=20, choices=PricingModel.choices, default=PricingModel.UNKNOWN,
    )

    # Flags
    is_featured = models.BooleanField(default=False)
    is_indexable = models.BooleanField(default=True)  # noindex if false

    # Quality
    quality_score = models.PositiveSmallIntegerField(default=0)

    # SEO
    meta_title = models.CharField(max_length=200, blank=True)
    meta_description = models.CharField(max_length=300, blank=True)

    # Search (Postgres-native)
    #
    # `search_index_text` is a denormalized blob updated from M2M-related
    # entities (use_cases, category names, capability labels) so we don't
    # have to JOIN at search time. Edited only by signals; do not write by hand.
    #
    # `search_vector` is the materialized tsvector built from `name`,
    # `verdict`, `short_description`, `long_description`, `developer_name`
    # and `search_index_text`. Refreshed via `refresh_search_vector(app_id)`.
    search_index_text = models.TextField(blank=True, default="")
    search_vector = SearchVectorField(null=True, blank=True)

    # Timestamps
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager()
    published = PublishedAppManager()

    class Meta:
        indexes = [
            models.Index(fields=["status", "-quality_score"]),
            models.Index(fields=["-first_seen_at"]),
            models.Index(fields=["is_featured", "-quality_score"]),
            GinIndex(fields=["search_vector"], name="app_search_vector_gin"),
            GinIndex(name="app_name_trgm_gin", fields=["name"],
                     opclasses=["gin_trgm_ops"]),
            GinIndex(name="app_dev_trgm_gin", fields=["developer_name"],
                     opclasses=["gin_trgm_ops"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(quality_score__gte=0) & Q(quality_score__lte=100),
                name="app_quality_score_range",
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self) -> str:
        return f"/apps/{self.slug}/"


class AppPlatform(models.Model):
    """How an `App` is available on a specific `Platform`.

    Top-level columns are platform-agnostic. Anything type-specific (MCP
    transport, ChatGPT app marketplace id, etc.) lives in `metadata` JSONField
    so we don't add a wide column for every listing type.
    """

    class CompatibilityStatus(TextChoices):
        SUPPORTED = "supported", "Supported"
        PARTIAL = "partial", "Partial"
        PREVIEW = "preview", "Preview"
        UNKNOWN = "unknown", "Unknown"

    class RegionAvailability(TextChoices):
        WORLDWIDE = "worldwide", "Worldwide"
        US_ONLY = "us_only", "US only"
        EU_ONLY = "eu_only", "EU only"
        EXCLUDES_EEA = "excludes_eea", "Excludes EEA"
        LIMITED = "limited", "Limited (see notes)"
        UNKNOWN = "unknown", "Unknown"

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="platform_links")
    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name="app_links")

    compatibility_status = models.CharField(
        max_length=20, choices=CompatibilityStatus.choices,
        default=CompatibilityStatus.SUPPORTED,
    )

    # Access & availability.
    # `supported_plans` is the single source of truth (the array form
    # supports "Plus and above" / multi-plan answers). No scalar
    # `required_plan` field — it duplicated this list and led to drift.
    supported_plans = models.JSONField(default=list, blank=True)  # ["Free", "Plus", "Pro"]
    region_availability = models.CharField(
        max_length=20, choices=RegionAvailability.choices,
        default=RegionAvailability.UNKNOWN,
    )
    supported_models = models.CharField(
        max_length=300, blank=True,
        help_text="Free text. Models the listing works with: 'gpt-5, gpt-5-mini' / 'claude-4-sonnet'.",
    )
    scope_summary = models.CharField(
        max_length=280, blank=True,
        help_text="Human summary of what permissions / scopes the integration requests.",
    )

    # Links
    official_directory_url = models.URLField(blank=True)
    install_url = models.URLField(blank=True)

    # Type-specific bag. Schema below is informational, not enforced.
    # For MCP-server listings, expected keys:
    #   protocol_version: "2025-03-26"
    #   transport: "stdio" | "sse" | "http" | "websocket"
    #   repository_url: "https://github.com/..."
    #   required_env_vars: ["GITHUB_TOKEN", "OPENAI_API_KEY"]
    #   install_command: "npx -y @org/server"
    # For ChatGPT App listings, expected keys:
    #   marketplace_listing_id: "..."
    #   auth_provider: "openai" | "third_party"
    metadata = models.JSONField(default=dict, blank=True)

    notes = models.CharField(max_length=300, blank=True)
    last_verified_on_platform_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("app", "platform")
        indexes = [models.Index(fields=["platform", "app"])]


class AppCategory(models.Model):
    app = models.ForeignKey(App, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    is_primary = models.BooleanField(default=False)

    class Meta:
        unique_together = ("app", "category")


class AppCapability(models.Model):
    class CapabilityValue(TextChoices):
        YES = "yes", "Yes"
        NO = "no", "No"
        UNKNOWN = "unknown", "Unknown"

    app = models.ForeignKey(App, on_delete=models.CASCADE)
    capability = models.ForeignKey(Capability, on_delete=models.CASCADE)
    value = models.CharField(max_length=10, choices=CapabilityValue.choices,
                             default=CapabilityValue.UNKNOWN)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ("app", "capability")


class AppUseCase(models.Model):
    app = models.ForeignKey(App, on_delete=models.CASCADE)
    use_case = models.ForeignKey(UseCase, on_delete=models.CASCADE)

    class Meta:
        unique_together = ("app", "use_case")
```

### 5.2. `apps/sources/models.py`

```python
class Source(models.Model):
    class SourceType(TextChoices):
        MANUAL = "manual"
        MCP_REGISTRY = "mcp_registry"
        SUBMISSION = "submission"
        CHATGPT_DIRECTORY = "chatgpt_directory"
        CLAUDE_CONNECTORS = "claude_connectors"

    app = models.ForeignKey("catalog.App", on_delete=models.CASCADE, related_name="sources")
    source_type = models.CharField(max_length=40, choices=SourceType.choices)
    source_url = models.URLField(blank=True)
    external_id = models.CharField(max_length=200, blank=True)
    payload = models.JSONField(default=dict, blank=True)  # raw normalized record
    fetched_at = models.DateTimeField(default=timezone.now)
    is_primary = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["source_type", "external_id"]),
            models.Index(fields=["app", "source_type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["source_type", "external_id"],
                name="source_dedupe_by_external_id",
                condition=~Q(external_id=""),
            ),
        ]
```

### 5.3. `apps/submissions/models.py`

```python
class Submission(models.Model):
    class Status(TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"

    app_name = models.CharField(max_length=200)
    app_url = models.URLField()
    developer_name = models.CharField(max_length=200, blank=True)
    listing_type_hint = models.CharField(max_length=80, blank=True)
    platforms_hint = models.JSONField(default=list, blank=True)
    short_description = models.CharField(max_length=280)
    long_description = models.TextField(blank=True)
    use_cases = models.JSONField(default=list, blank=True)
    capabilities = models.JSONField(default=dict, blank=True)
    pricing_model = models.CharField(max_length=20, blank=True)
    launch_status = models.CharField(max_length=20, blank=True)
    repo_url = models.URLField(blank=True)
    submitter_email = models.EmailField()
    submitter_ip = models.GenericIPAddressField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    rejection_reason = models.CharField(max_length=300, blank=True)
    resulting_app = models.ForeignKey(
        "catalog.App", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="submissions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "-created_at"])]


class ClaimRequest(models.Model):
    class Status(TextChoices):
        PENDING = "pending"
        VERIFIED = "verified"
        REJECTED = "rejected"

    class AutoCheckStatus(TextChoices):
        UNCHECKED = "unchecked", "Not yet auto-checked"
        PASSED = "passed", "Auto-check passed"
        FAILED = "failed", "Auto-check failed"
        INCONCLUSIVE = "inconclusive", "Auto-check inconclusive"

    app = models.ForeignKey("catalog.App", on_delete=models.CASCADE, related_name="claims")
    claimer_name = models.CharField(max_length=200)
    claimer_email = models.EmailField()
    proof_type = models.CharField(max_length=40)   # "domain_email", "dns_txt", "repo_readme", ...
    proof_url = models.URLField(blank=True)
    proof_token = models.CharField(max_length=64, blank=True)  # for DNS/README verification
    comment = models.TextField(blank=True)

    # Editor's decision — only an admin action flips this to VERIFIED.
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Async automated probe — informs the editor but never decides on its own.
    auto_check_status = models.CharField(
        max_length=20, choices=AutoCheckStatus.choices, default=AutoCheckStatus.UNCHECKED,
    )
    auto_check_log = models.TextField(blank=True)   # raw findings for the editor
    auto_check_at = models.DateTimeField(null=True, blank=True)

    rejection_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="claims_decided",
    )

    class Meta:
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["auto_check_status", "status"]),
        ]
```

### 5.4. `apps/sources/models.py` — link-check history

State needed to drive automatic deprecation (business.md § 11.3) is not derivable from `last_checked_at` alone — we need to remember consecutive failures and which URL was probed:

```python
class LinkCheckResult(models.Model):
    class Target(TextChoices):
        OFFICIAL = "official"
        INSTALL = "install"
        DIRECTORY = "directory"
        REPO = "repo"

    app = models.ForeignKey("catalog.App", on_delete=models.CASCADE,
                            related_name="link_checks")
    target = models.CharField(max_length=20, choices=Target.choices)
    url = models.URLField()
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    ok = models.BooleanField()
    error_message = models.CharField(max_length=300, blank=True)
    checked_at = models.DateTimeField(auto_now_add=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["app", "target", "-checked_at"]),
            models.Index(fields=["-checked_at"]),
        ]


class LinkHealth(models.Model):
    """Rolling summary per (app, target). Updated by link-check task.
    Drives the 'auto-deprecate after 7 consecutive failures' rule.
    """
    app = models.ForeignKey("catalog.App", on_delete=models.CASCADE,
                            related_name="link_health")
    target = models.CharField(max_length=20, choices=LinkCheckResult.Target.choices)
    url = models.URLField()
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("app", "target")
        indexes = [models.Index(fields=["-consecutive_failures"])]
```

The link-check task (`apps.sources.tasks.check_single_app_link`) writes one `LinkCheckResult` per probe and updates the corresponding `LinkHealth` row. When `consecutive_failures` reaches 7, a separate task flips `App.launch_status` to `DEPRECATED` and notifies the editor.

### 5.5. `apps/analytics/models.py`

```python
class ClickEvent(models.Model):
    class Target(TextChoices):
        OFFICIAL = "official"
        INSTALL = "install"
        REPO = "repo"

    app = models.ForeignKey("catalog.App", on_delete=models.CASCADE, related_name="clicks")
    target = models.CharField(max_length=20, choices=Target.choices)
    referrer = models.URLField(blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    ip_hash = models.CharField(max_length=64, blank=True)  # one-way hash, not raw IP
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["app", "-created_at"]),
            models.Index(fields=["-created_at"]),
        ]
```

### 5.6. `apps/catalog/services.py`

```python
from .models import App, AppCapability


QUALITY_RULES = [
    # (predicate(app) -> bool, delta)

    # Three independent trust axes — each contributes separately.
    (lambda a: a.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL, +15),
    (lambda a: a.editorial_review_status == App.EditorialReviewStatus.REVIEWED, +10),
    (lambda a: a.developer_claim_status == App.DeveloperClaimStatus.CLAIMED, +10),

    (lambda a: bool(a.official_page_url) and bool(a.install_url), +15),
    (lambda a: bool(a.verdict), +10),
    (lambda a: a.use_cases.count() >= 5, +10),
    (lambda a: AppCapability.objects.filter(app=a)
                                    .exclude(value=AppCapability.CapabilityValue.UNKNOWN)
                                    .count() >= 5, +10),
    (lambda a: bool(a.developer_name) and a.developer_name.lower() != "unknown", +5),
    (lambda a: bool(a.logo), +5),
    (lambda a: a.last_checked_at and (timezone.now() - a.last_checked_at).days < 30, +5),
    (lambda a: bool(a.repo_url), +5),
    (lambda a: a.launch_status == App.LaunchStatus.DEPRECATED, -10),
    (lambda a: not a.sources.exclude(source_type="mcp_registry").exists(), -10),
    # Recent link-check failures drag the score down.
    (lambda a: a.link_health.filter(consecutive_failures__gte=1).exists(), -5),
]


def recalc_quality_score(app: App) -> int:
    score = sum(delta for predicate, delta in QUALITY_RULES if predicate(app))
    score = max(0, min(100, score))
    if app.quality_score != score:
        App.objects.filter(pk=app.pk).update(quality_score=score)
    return score


def transition_to_published(app: App, editor) -> None:
    """Strict gatekeeper for publishing an App."""
    errors = []
    if len(app.short_description) < 60:
        errors.append("short_description must be >= 60 chars")
    if not app.platforms.exists():
        errors.append("at least one platform required")
    if not app.categories.exists():
        errors.append("at least one category required")
    explicit_caps = AppCapability.objects.filter(app=app).exclude(
        value=AppCapability.CapabilityValue.UNKNOWN
    ).count()
    if explicit_caps < 3:
        errors.append("at least 3 explicit capabilities required")
    if not (app.official_page_url or app.install_url):
        errors.append("official_page_url or install_url required")
    if app.editorial_review_status != App.EditorialReviewStatus.REVIEWED:
        errors.append("editorial review required before publishing")
    if app.platform_verification_status == App.PlatformVerificationStatus.UNKNOWN:
        errors.append("platform_verification_status must be official or not_listed")
    if app.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL:
        has_directory_url = app.platform_links.exclude(
            official_directory_url=""
        ).exists()
        if not has_directory_url:
            errors.append("official platforms need an official_directory_url on AppPlatform")
    if errors:
        raise ValueError("Cannot publish: " + "; ".join(errors))
    app.status = App.AppStatus.PUBLISHED
    app.last_checked_at = timezone.now()
    app.save(update_fields=["status", "last_checked_at"])
    recalc_quality_score(app)
```

---

## 6. Search layer (PostgreSQL)

The search layer is intentionally implemented inside Postgres. No external search service in MVP.

There are three concerns:

1. **Full-text search** — match the query against the name, verdict, descriptions and use-case text.
2. **Typo tolerance** — match misspellings on the app name (`pg_trgm`).
3. **Faceted filtering** — return counts per platform/category/capability/pricing/etc., scoped to the current filter set.

All three are doable in plain Postgres with the right indexes and a single materialized `search_vector` column.

### 6.1. The `search_vector` column

The `App.search_vector` field (declared in § 5.1) is a `tsvector` built from a weighted combination of the app's textual fields plus a denormalized `search_index_text` blob populated from related entities.

`apps/search/vector.py`:

```python
from django.contrib.postgres.search import SearchVector
from django.db import transaction
from django.db.models import F

from apps.catalog.models import (
    App, AppCapability, Category, Capability, UseCase,
)


SEARCH_VECTOR_EXPR = (
    SearchVector("name", weight="A", config="english")
    + SearchVector("verdict", weight="A", config="english")
    + SearchVector("short_description", weight="B", config="english")
    + SearchVector("developer_name", weight="B", config="english")
    + SearchVector("search_index_text", weight="B", config="english")
    + SearchVector("long_description", weight="C", config="english")
)


def build_search_index_text(app: App) -> str:
    """Denormalize M2M-related text into one blob.

    We deliberately materialize this instead of JOINing at query time:
    full-text matches over a single column are faster, and we don't have
    to think about which JOINs `SearchVector` adds under the hood.
    """
    parts: list[str] = []
    parts.extend(app.use_cases.values_list("title", flat=True))
    parts.extend(app.categories.values_list("name", flat=True))
    parts.extend(
        Capability.objects.filter(
            appcapability__app=app,
            appcapability__value=AppCapability.CapabilityValue.YES,
        ).values_list("label", flat=True)
    )
    parts.extend(app.platforms.values_list("name", flat=True))
    parts.extend(app.listing_types.values_list("name", flat=True))
    return " | ".join(p for p in parts if p)


def refresh_search_vector(app_id: int) -> None:
    """Update `search_index_text`, THEN recompute `search_vector`.

    These must be two separate UPDATE statements. Postgres evaluates all
    expressions in a single `UPDATE ... SET a=..., b=...` against the
    pre-update row, so building `search_vector` from the column inside the
    same statement would consume the *old* `search_index_text` value. The
    end result would be `search_vector` perpetually one step behind.
    """
    try:
        app = App.objects.get(pk=app_id)
    except App.DoesNotExist:
        return

    text = build_search_index_text(app)

    # Step 1: write the denormalized text into the row.
    App.objects.filter(pk=app_id).update(search_index_text=text)

    # Step 2: rebuild tsvector now that the row already holds the new text.
    App.objects.filter(pk=app_id).update(search_vector=SEARCH_VECTOR_EXPR)
```

Why a denormalized `search_index_text` blob: `SearchVector` on M2M-related fields requires per-row subqueries during search and complicates GIN index usage. Materializing the text once, on save, keeps the search index a single-column GIN — simple, fast, and easy to reason about.

### 6.2. When we refresh

| Trigger | Mechanism |
|---|---|
| `App.save()` | `post_save` signal → `transaction.on_commit(refresh_search_vector.delay(app_id))` |
| `App` related M2M change (categories, platforms, capabilities, use_cases, listing_types) | `m2m_changed` signal → same task |
| Editor action *Refresh search index* | direct call (synchronous) |
| Daily safety-net | beat-scheduled `refresh_search_vectors_batch` over all rows |

A Celery task is used so the request that triggered the save doesn't block on the update:

```python
# apps/search/tasks.py
from celery import shared_task
from .vector import refresh_search_vector, build_search_index_text, SEARCH_VECTOR_EXPR
from apps.catalog.models import App


@shared_task
def refresh_search_vector_task(app_id: int) -> None:
    refresh_search_vector(app_id)


@shared_task
def refresh_search_vectors_batch() -> None:
    """Safety net: walk all rows once a day."""
    for app_id in App.objects.values_list("pk", flat=True).iterator(chunk_size=500):
        refresh_search_vector(app_id)
```

Signal wiring lives in `apps/catalog/signals.py` (full code in § 12).

### 6.3. Query — full-text + trigram fallback

`apps/search/views.py`:

```python
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.contrib.postgres.search import TrigramSimilarity
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Value
from django.shortcuts import render

from apps.catalog.models import App
from .filters import apply_filters, parse_sort
from .facets import compute_facets


PAGE_SIZE = 24
TRIGRAM_THRESHOLD = 0.25


def app_search(request):
    q = (request.GET.get("q") or "").strip()
    sort = parse_sort(request.GET.get("sort"), has_query=bool(q))

    base = App.published.all()
    qs = apply_filters(base, request.GET)

    if q:
        query = SearchQuery(q, search_type="websearch", config="english")
        qs = (
            qs.annotate(
                rank=SearchRank(F("search_vector"), query),
                name_sim=TrigramSimilarity("name", q),
                dev_sim=TrigramSimilarity("developer_name", q),
            )
            .filter(
                Q(search_vector=query)
                | Q(name_sim__gt=TRIGRAM_THRESHOLD)
                | Q(dev_sim__gt=TRIGRAM_THRESHOLD)
            )
            .order_by(*sort.django_order_by(score_fields=["-rank", "-name_sim"]))
        )
    else:
        qs = qs.order_by(*sort.django_order_by())

    paginator = Paginator(qs.distinct(), PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page") or 1)

    facets = compute_facets(base, request.GET)

    template = ("partials/catalog_results.html"
                if request.headers.get("HX-Request")
                else "catalog/list.html")
    return render(request, template, {
        "q": q,
        "page_obj": page,
        "facets": facets,
        "active_filters": request.GET,
        "sort": sort,
    })
```

Key choices:

- **`search_type="websearch"`** accepts user-friendly syntax (`"exact phrase"`, `-exclude`, `OR`) without us writing a parser.
- **Trigram fallback** catches typos in the name and developer ("anthopic" → Anthropic, "claud" → Claude). The threshold 0.25 is a starting point — tune by eyeballing real queries.
- **`SearchRank` + `name_sim`** are combined in the ordering when the user typed a query; otherwise we fall back to `quality_score`, `first_seen_at`, etc., according to the chosen sort.
- **`.distinct()`** is required because the M2M filters can multiply rows.

### 6.4. Filter parsing

`apps/search/filters.py`:

```python
from dataclasses import dataclass
from django.db.models import QuerySet


@dataclass(frozen=True)
class Sort:
    key: str
    label: str

    def django_order_by(self, score_fields: list[str] | None = None) -> list[str]:
        match self.key:
            case "newest":
                return ["-first_seen_at"]
            case "quality":
                return ["-quality_score", "-first_seen_at"]
            case "featured":
                return ["-is_featured", "-quality_score", "-first_seen_at"]
            case "relevance":
                return (score_fields or []) + ["-quality_score", "-first_seen_at"]
            case _:
                return ["-quality_score", "-first_seen_at"]


SORT_OPTIONS = {
    "relevance": Sort("relevance", "Relevance"),
    "newest":    Sort("newest",    "Newest"),
    "quality":   Sort("quality",   "Quality"),
    "featured":  Sort("featured",  "Featured"),
}


def parse_sort(raw: str | None, *, has_query: bool) -> Sort:
    return SORT_OPTIONS.get(raw or "", SORT_OPTIONS["relevance" if has_query else "quality"])


FILTERABLE_FACETS = {
    # GET param            model lookup (used for the simple "value in list" case)
    "listing_types":        "listing_types__slug__in",
    "categories":           "categories__slug__in",
    "pricing":              "pricing_model__in",
    "launch":               "launch_status__in",
    "platform_verification":"platform_verification_status__in",
    "editorial_review":     "editorial_review_status__in",
    "developer_claim":      "developer_claim_status__in",
    # "capability"  — apply_capability_filter
    # "platforms"   — apply_platform_region_filter (must share an AppPlatform row with region)
    # "region"      — apply_platform_region_filter (same reason)
}


def apply_filters(qs: QuerySet, params, *, exclude: str | None = None) -> QuerySet:
    for key, lookup in FILTERABLE_FACETS.items():
        if key == exclude:
            continue
        values = params.getlist(key)
        if values:
            qs = qs.filter(**{lookup: values})

    if exclude != "capability":
        qs = apply_capability_filter(qs, params)
    # Region + platform must match on the SAME AppPlatform row (see below).
    qs = apply_platform_region_filter(qs, params, exclude=exclude)
    return qs


def apply_capability_filter(qs: QuerySet, params) -> QuerySet:
    """Capability filter accepts pairs `capability=<key>:<value>`.

    `<value>` ∈ {"yes", "no", "unknown", "not_yes", "not_no", "not_unknown"}.

    Examples:
      ?capability=write_actions:yes          → can take actions
      ?capability=write_actions:no           → read-only
      ?capability=local_setup_required:no    → does not require local setup
      ?capability=open_source:yes&capability=remote_available:yes
                                             → both must hold
    """
    pairs = params.getlist("capability")
    if not pairs:
        return qs
    for pair in pairs:
        if ":" not in pair:
            continue
        cap_key, value = pair.split(":", 1)
        if value.startswith("not_"):
            qs = qs.exclude(
                appcapability__capability__key=cap_key,
                appcapability__value=value[4:],
            )
        else:
            qs = qs.filter(
                appcapability__capability__key=cap_key,
                appcapability__value=value,
            )
    return qs


def apply_platform_region_filter(qs: QuerySet, params, *, exclude=None) -> QuerySet:
    """Filter by platform and/or region.

    The naive `qs.filter(platforms__slug__in=[…]).filter(
    platform_links__region_availability__in=[…])` is WRONG when both are
    set: Django joins `AppPlatform` twice and the conditions can match
    DIFFERENT rows of the same app. An app available on Claude (US only)
    and on MCP (worldwide) would then match `platform=mcp + region=us_only`.

    The fix is one correlated EXISTS over a single `AppPlatform` row:
    """
    from django.db.models import Exists, OuterRef
    from apps.catalog.models import AppPlatform

    platforms = params.getlist("platforms") if exclude != "platforms" else []
    regions = params.getlist("region") if exclude != "region" else []
    if not platforms and not regions:
        return qs

    inner = AppPlatform.objects.filter(app=OuterRef("pk"))
    if platforms:
        inner = inner.filter(platform__slug__in=platforms)
    if regions:
        inner = inner.filter(region_availability__in=regions)
    return qs.filter(Exists(inner))
```

Note: when platforms-only or region-only is set, the EXISTS still gives a correct (and equivalent) result; using it unconditionally keeps the code paths consistent.

If you also want platform / region facet **counts** to share this constraint, the same EXISTS pattern is reused inside `compute_facets` for those two facets — see § 6.5.

`apply_filters` accepts `exclude=` so facet aggregation can compute the count for each facet *as if the user removed only that facet* — the standard "drill-down with sticky facet" UX.

`capability` filtering must work in three modes — there's a real difference between "we know this tool does not need local setup" (`local_setup_required=no`) and "we don't know yet" (`local_setup_required=unknown`). Treating capabilities as a flat list of "yes" flags would silently hide tools we haven't verified, which is the opposite of the catalog's job.

### 6.5. Facet aggregation

`apps/search/facets.py`:

```python
from django.db.models import Count, Q

from apps.catalog.models import App
from .filters import apply_filters


FACET_DEFS = [
    # (param key, group field on App, label field — None means group value is the label)
    ("listing_types",         "listing_types__slug",            "listing_types__name"),
    ("categories",            "categories__slug",               "categories__name"),
    ("pricing",               "pricing_model",                  None),
    ("launch",                "launch_status",                  None),
    ("platform_verification", "platform_verification_status",   None),
    ("editorial_review",      "editorial_review_status",        None),
    ("developer_claim",       "developer_claim_status",         None),
    # "platforms" and "region" facets are computed below with the same
    # EXISTS pattern as the filter, so the counts honour the
    # same-AppPlatform-row constraint.
]


def compute_facets(base_qs, params):
    """Return facet counts. Each facet is computed against the base qs
    filtered by all OTHER active filters (sticky-facet UX).
    """
    facets = {}
    for key, group_field, label_field in FACET_DEFS:
        qs = apply_filters(base_qs, params, exclude=key)
        values_args = [group_field] + ([label_field] if label_field else [])
        rows = (
            qs.values(*values_args)
              .exclude(**{f"{group_field}__isnull": True})
              .annotate(count=Count("id", distinct=True))
              .order_by("-count", group_field)
        )
        facets[key] = [
            {
                "value": row[group_field],
                "label": row.get(label_field) if label_field else row[group_field],
                "count": row["count"],
            }
            for row in rows
        ]

    # Capabilities: every (capability_key, value) pair gets its own count.
    # The template groups by capability_key and shows three counters per row:
    # yes / no / unknown — see business.md § 8.2.
    cap_qs = apply_filters(base_qs, params, exclude="capability")
    cap_rows = (
        cap_qs.values("appcapability__capability__key",
                      "appcapability__capability__label",
                      "appcapability__value")
              .exclude(appcapability__capability__key__isnull=True)
              .annotate(count=Count("id", distinct=True))
              .order_by("appcapability__capability__key",
                        "appcapability__value")
    )
    grouped: dict[str, dict] = {}
    for r in cap_rows:
        key = r["appcapability__capability__key"]
        grouped.setdefault(key, {
            "key": key,
            "label": r["appcapability__capability__label"],
            "counts": {"yes": 0, "no": 0, "unknown": 0},
        })["counts"][r["appcapability__value"]] = r["count"]
    facets["capabilities"] = list(grouped.values())

    # Platforms + regions: counted from the same AppPlatform row, so that
    # filtering by platform AND region matches identically to the search
    # path. We compute these via the through model directly.
    from apps.catalog.models import AppPlatform
    base_app_ids = apply_filters(base_qs, params, exclude="platforms").values("pk")
    ap = (
        AppPlatform.objects.filter(app__in=base_app_ids)
        .values("platform__slug", "platform__name")
        .annotate(count=Count("app_id", distinct=True))
        .order_by("-count")
    )
    facets["platforms"] = [
        {"value": r["platform__slug"], "label": r["platform__name"], "count": r["count"]}
        for r in ap
    ]

    base_app_ids = apply_filters(base_qs, params, exclude="region").values("pk")
    region_rows = (
        AppPlatform.objects.filter(app__in=base_app_ids)
        .exclude(region_availability="unknown")
        .values("region_availability")
        .annotate(count=Count("app_id", distinct=True))
        .order_by("-count")
    )
    facets["region"] = [
        {"value": r["region_availability"], "label": r["region_availability"],
         "count": r["count"]}
        for r in region_rows
    ]

    return facets
```

For MVP catalog sizes (< 20K rows) each facet aggregation runs in 5–30 ms on Postgres with the indexes from § 4.4. Cache the home/platform/category page results (§ 13) and the load profile stays comfortable.

### 6.6. When Postgres FTS stops being enough

We migrate to a dedicated engine **only** when one of these is true, not earlier:

- Catalog exceeds ~50K rows AND p95 search latency consistently > 200 ms.
- Editorial team needs query analytics / synonyms / per-tenant boosting that Postgres can't express cleanly.
- We add multi-language search and `unaccent` + per-language `tsconfig` is no longer enough.

When that happens, the contract of `apps/search/views.py` stays the same — only `vector.py`, `facets.py` and the indexer are rewritten.

---

## 7. HTMX patterns

### 7.1. Faceted catalog page

`templates/catalog/list.html` (excerpt):

```html
<form id="filters"
      hx-get="{% url 'catalog:list' %}"
      hx-target="#results"
      hx-trigger="change, keyup delay:300ms from:#q"
      hx-push-url="true"
      hx-include="this">
  <input id="q" name="q" type="search" placeholder="Search by task…">
  {% include "partials/facets.html" with facets=facets %}
</form>

<section id="results">
  {% include "partials/catalog_results.html" %}
</section>
```

`partials/catalog_results.html`:

```html
<div class="grid grid-cols-1 md:grid-cols-3 gap-6">
  {% for app in page_obj %}
    {% include "partials/app_card.html" with app=app %}
  {% empty %}
    {% include "partials/empty_state.html" %}
  {% endfor %}
</div>
{% include "partials/pagination.html" with page_obj=page_obj %}
```

### 7.2. Submit form

`templates/submissions/submit.html`:

```html
<form hx-post="{% url 'submissions:submit' %}"
      hx-target="this"
      hx-swap="outerHTML"
      class="space-y-4">
  {% csrf_token %}
  {{ form.as_p }}
  {{ turnstile_widget|safe }}
  <button type="submit" class="btn-primary">Submit listing</button>
</form>
```

View:

```python
def submit(request):
    if request.method == "POST":
        form = SubmissionForm(request.POST)
        if form.is_valid() and verify_turnstile(request):
            submission = form.save(commit=False)
            submission.submitter_ip = get_client_ip(request)
            submission.save()
            send_submission_notification.delay(submission.pk)
            return render(request, "submissions/_thanks.html", {"submission": submission})
    else:
        form = SubmissionForm()
    return render(request, "submissions/submit.html",
                  {"form": form, "turnstile_widget": turnstile_widget()})
```

`_thanks.html` is the partial that replaces the form on successful submit.

### 7.3. Pagination

Two flavors, used in different places:

- **Catalog** uses classic `?page=N` pagination — friendlier to crawlers.
- **Homepage “Newly added” strip** uses `hx-trigger="revealed"` for progressive load:

```html
<div hx-get="{% url 'catalog:newly_added' %}?cursor={{ next_cursor }}"
     hx-trigger="revealed"
     hx-swap="outerHTML">
  Loading…
</div>
```

---

## 8. Routing

### 8.1. URL → view map

| URL | View | Template |
|---|---|---|
| `/` | `catalog.views.home` | `home.html` |
| `/apps/` | `search.views.app_search` (mounted under the `catalog` URL namespace as `catalog:list`) | `catalog/list.html` |
| `/apps/<slug:slug>/` | `catalog.views.app_detail` | `catalog/detail.html` |
| `/<str:public_path>/` for each `Platform.public_path` (`chatgpt-apps`, `claude-connectors`, `mcp-servers`, `gemini-apps`, ...) | `catalog.views.platform_page` | `catalog/platform.html` |
| `/apps/<slug:category>/` | `catalog.views.category_page` | `catalog/category.html` |
| `/<str:public_path>/<slug:category>/` — resolved via `Platform.public_path` | `catalog.views.cross_page` | `catalog/cross.html` |
| `/best/<slug>/` | `editorial.views.collection` | `editorial/collection.html` |
| `/vs/<slug>/` | `editorial.views.comparison` | `editorial/comparison.html` |
| `/blog/`, `/blog/<slug>/` | `editorial.views.{blog_list,blog_post}` | `editorial/blog_*.html` |
| `/submit/` | `submissions.views.submit` | `submissions/submit.html` |
| `/claim/<slug:app_slug>/` | `submissions.views.claim` | `submissions/claim.html` |
| `/go/<slug:app_slug>/<str:target>/` | `analytics.views.outbound_redirect` | (redirect) |
| `/subscribe/` | `newsletter.views.subscribe` | `newsletter/subscribe.html` |
| `/sitemap.xml` | sitemap index | (xml) |
| `/robots.txt` | `seo.views.robots` | (text) |
| `/healthz/` | `core.healthcheck.view` | (json) |

### 8.2. Slug rules

- `App.slug` is global-unique.
- Generated as `slugify(name)`; collisions get a `-2`, `-3` suffix.
- `Platform.slug` (`"claude"`) is the internal identifier; the user-facing URL segment lives in `Platform.public_path` (`"claude-connectors"`). Resolver does `Platform.objects.get(public_path=…)`. We don't auto-derive paths from slug — there is no rule that turns "claude" into "claude-connectors" cleanly.
- Cross-pages `/<public_path>/<category-slug>/` are resolved by looking up `Platform.public_path` and `Category.slug` separately; both must exist and be public.
- Slugs and public paths are immutable after the first release. Renames create redirects (`Redirect` model from `django.contrib.redirects`).

### 8.3. Outbound redirect

The link a user clicks for *Open in directory* never sends them directly to the external URL — instead:

```
GET /go/{app_slug}/official/  →  302 to App.official_page_url, records ClickEvent
GET /go/{app_slug}/install/   →  302 to App.install_url
GET /go/{app_slug}/repo/      →  302 to App.repo_url
```

This is what feeds the *Trending* block. The view is async-safe and rate-limited per IP hash.

---

## 9. Sources & ingest

### 9.1. `BaseSource` interface

```python
class AppDraft(BaseModel):
    """In-memory normalized record. Not saved yet."""
    name: str
    slug_hint: str
    short_description: str
    long_description: str = ""
    developer_name: str = ""
    developer_url: str = ""
    official_page_url: str = ""
    install_url: str = ""
    repo_url: str = ""
    platforms: list[str] = []      # platform slugs
    listing_types: list[str] = []  # listing-type slugs
    categories: list[str] = []
    capabilities: dict[str, str] = {}  # cap_key -> "yes"|"no"|"unknown"
    pricing_model: str = "unknown"
    launch_status: str = "live"
    external_id: str = ""
    raw_payload: dict = {}

    # Per-AppPlatform fields. The directory_url is critical: when
    # platform_verification_status is OFFICIAL, the publish-gate in
    # `transition_to_published` requires at least one AppPlatform with
    # `official_directory_url` filled. Sources that imply OFFICIAL status
    # (MCP Registry, ChatGPT App Directory, Claude Connectors) MUST
    # populate this — otherwise published cards land in admin "needs
    # directory_url" queue.
    platform_metadata: dict = {}        # arbitrary type-specific blob
    official_directory_url: str = ""    # canonical URL inside the source directory
    supported_plans: list[str] = []
    region_availability: str = "unknown"
    scope_summary: str = ""


class BaseSource:
    source_type: str

    def iter_drafts(self) -> Iterable[AppDraft]:
        raise NotImplementedError
```

### 9.2. `MCPRegistrySource`

```python
class MCPRegistrySchemaError(Exception):
    """Raised when a record's shape doesn't match the version we know."""


class MCPRegistrySource(BaseSource):
    """Ingest from the official MCP Registry.

    The Registry is officially in PREVIEW: schema may change, data may be
    reset, IDs may rotate. We design the source for those realities:

    - Every HTTP call uses retry + exponential backoff (3 attempts).
    - We tolerate single-record schema mismatches: bad rows are routed to
      an `unparsed_queue`, the batch keeps going.
    - The full `raw_payload` is always stored on Source, so we can re-parse
      historical records when our normalizer is updated.
    - We record the schema_version we observed; if it changes globally,
      we surface a Sentry alert instead of silently corrupting data.
    """

    source_type = Source.SourceType.MCP_REGISTRY
    BASE_URL = "https://registry.modelcontextprotocol.io/v1"
    KNOWN_SCHEMA_VERSIONS = {"1.0", "1.1"}  # update as the registry evolves
    PAGE_SIZE = 100

    def __init__(self, http=None):
        self.http = http or self._build_session()
        self.unparsed: list[dict] = []
        self.observed_schema_versions: set[str] = set()

    def _build_session(self):
        s = requests.Session()
        adapter = HTTPAdapter(max_retries=Retry(
            total=3, backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        ))
        s.mount("https://", adapter)
        return s

    def iter_drafts(self) -> Iterable[AppDraft]:
        cursor = None
        while True:
            try:
                resp = self.http.get(
                    f"{self.BASE_URL}/servers",
                    params={"cursor": cursor, "limit": self.PAGE_SIZE},
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.RequestException:
                logger.exception("mcp_registry_page_fetch_failed", extra={"cursor": cursor})
                # Stop the batch; the next scheduled run will pick up.
                # Do NOT raise — we want a clean Sentry breadcrumb, not a crash loop.
                return

            payload = resp.json()
            self.observed_schema_versions.add(payload.get("schema_version", "unknown"))
            for record in payload.get("servers", []):
                try:
                    yield self._normalize(record)
                except MCPRegistrySchemaError as e:
                    self.unparsed.append({"record": record, "error": str(e)})
                    logger.warning("mcp_registry_unparsed", extra={"id": record.get("id")})
                    continue
            cursor = payload.get("next_cursor")
            if not cursor:
                break

    def _normalize(self, record: dict) -> AppDraft:
        try:
            name = record["name"]
            external_id = record["id"]
        except KeyError as e:
            raise MCPRegistrySchemaError(f"missing required field {e}") from None

        transports = record.get("transports", {}) or {}
        return AppDraft(
            name=name,
            slug_hint=slugify(name)[:200],
            short_description=(record.get("description") or "")[:280],
            long_description=record.get("description") or "",
            developer_name=(record.get("publisher") or {}).get("name", ""),
            developer_url=(record.get("publisher") or {}).get("url", ""),
            official_page_url=record.get("homepage") or "",
            install_url=(record.get("install") or {}).get("url", ""),
            repo_url=(record.get("repository") or {}).get("url", ""),
            platforms=["mcp"],
            listing_types=["mcp-server"],
            capabilities={
                "remote_available":
                    "yes" if transports.get("http") or transports.get("sse") else "unknown",
                "local_setup_required":
                    "yes" if transports.get("stdio") else "unknown",
                "open_source":
                    "yes" if record.get("repository") else "unknown",
            },
            external_id=external_id,
            raw_payload=record,
            # The MCP Registry IS a platform directory in our model — the
            # canonical URL inside the registry is what `AppPlatform.
            # official_directory_url` must hold for the publish-gate to
            # pass. Without this, every draft from registry would be
            # blocked at publish-time despite platform_verification=OFFICIAL.
            official_directory_url=(
                f"{MCPRegistrySource.BASE_URL}/servers/{external_id}"
            ),
            # platform-level metadata kept for AppPlatform.metadata
            platform_metadata={
                "protocol_version": record.get("protocol_version"),
                "transport": next((t for t in ("stdio", "sse", "http", "websocket")
                                   if transports.get(t)), None),
                "repository_url": (record.get("repository") or {}).get("url"),
                "install_command": (record.get("install") or {}).get("command"),
                "required_env_vars": (record.get("install") or {}).get("env", []),
            },
        )
```

`AppDraft` carries everything needed to produce a complete `AppPlatform` row:
`platform_metadata` (type-specific JSON), `official_directory_url`,
`supported_plans`, `region_availability`, `scope_summary`. `upsert_app_from_draft`
unpacks all of them into the `AppPlatform` row(s) it creates.

### 9.3. Ingest task

```python
@shared_task(bind=True, max_retries=2, default_retry_delay=600)
def ingest_mcp_registry(self):
    source = MCPRegistrySource()
    new, updated, skipped, errors = 0, 0, 0, 0
    for draft in source.iter_drafts():
        try:
            outcome = upsert_app_from_draft(draft, source.source_type)
        except Exception:
            errors += 1
            logger.exception("mcp_registry_upsert_failed",
                             extra={"external_id": draft.external_id})
            continue
        if outcome == "new": new += 1
        elif outcome == "updated": updated += 1
        else: skipped += 1

    unknown_versions = source.observed_schema_versions - MCPRegistrySource.KNOWN_SCHEMA_VERSIONS
    if unknown_versions:
        # Surface to Sentry — schema may have changed under us.
        logger.error("mcp_registry_unknown_schema_version",
                     extra={"versions": list(unknown_versions)})

    # Persist `unparsed` so an editor can inspect bad rows in admin.
    if source.unparsed:
        UnparsedRegistryRecord.objects.bulk_create([
            UnparsedRegistryRecord(payload=item["record"], error=item["error"])
            for item in source.unparsed
        ])

    logger.info("mcp_registry_ingest_done", extra={
        "new": new, "updated": updated, "skipped": skipped,
        "errors": errors, "unparsed": len(source.unparsed),
        "schema_versions": list(source.observed_schema_versions),
    })


def upsert_app_from_draft(draft: AppDraft, source_type: str) -> str:
    existing = Source.objects.filter(source_type=source_type, external_id=draft.external_id).first()
    if existing:
        # Update only safe fields; never overwrite editor edits to status/verdict/categories
        app = existing.app
        for field in ("long_description", "official_page_url", "install_url", "repo_url"):
            if not getattr(app, field):
                setattr(app, field, getattr(draft, field) or "")
        existing.payload = draft.raw_payload
        existing.fetched_at = timezone.now()
        existing.save(update_fields=["payload", "fetched_at"])
        app.save()
        return "updated"

    if soft_duplicate := find_soft_duplicate(draft):
        Source.objects.create(app=soft_duplicate, source_type=source_type,
                              external_id=draft.external_id, payload=draft.raw_payload)
        return "skipped"

    with transaction.atomic():
        app = App.objects.create(
            name=draft.name,
            slug=unique_slug(draft.slug_hint),
            short_description=draft.short_description,
            long_description=draft.long_description,
            developer_name=draft.developer_name,
            developer_url=draft.developer_url,
            official_page_url=draft.official_page_url,
            install_url=draft.install_url,
            repo_url=draft.repo_url,
            status=App.AppStatus.DRAFT,
            # The MCP Registry IS an official platform directory. So presence
            # there means platform_verification = OFFICIAL. But editorial
            # review and developer claim are independent and start empty.
            platform_verification_status=(
                App.PlatformVerificationStatus.OFFICIAL
                if source_type == Source.SourceType.MCP_REGISTRY
                else App.PlatformVerificationStatus.UNKNOWN
            ),
            editorial_review_status=App.EditorialReviewStatus.UNREVIEWED,
            developer_claim_status=App.DeveloperClaimStatus.UNCLAIMED,
        )
        attach_platforms(app, draft)
        attach_listing_types(app, draft.listing_types)
        attach_capabilities(app, draft.capabilities)
        Source.objects.create(app=app, source_type=source_type,
                              external_id=draft.external_id, payload=draft.raw_payload,
                              is_primary=True)
    return "new"
```

### 9.4. attach_* helpers

```python
def attach_platforms(app: App, draft: AppDraft) -> None:
    """Create/update one AppPlatform row per platform slug in the draft,
    filling all per-platform fields so the publish-gate (which insists on
    `official_directory_url` when platform_verification_status=OFFICIAL)
    won't reject the row later.
    """
    for slug in draft.platforms:
        platform = Platform.objects.get(slug=slug)
        AppPlatform.objects.update_or_create(
            app=app, platform=platform,
            defaults={
                "official_directory_url": draft.official_directory_url,
                "supported_plans": draft.supported_plans,
                "region_availability": draft.region_availability or "unknown",
                "scope_summary": draft.scope_summary,
                "metadata": draft.platform_metadata or {},
                "last_verified_on_platform_at": timezone.now(),
            },
        )


def attach_capabilities(app: App, capabilities: dict[str, str]) -> None:
    """Invariant: every known Capability gets an AppCapability row.

    Rationale: capability filters support 'unknown' as an explicit
    value (yes/no/unknown). For that to work the row must exist —
    otherwise `?capability=local_setup_required:unknown` would silently
    miss apps where the capability was never set.

    Cards created without explicit values default each capability to
    `unknown`; the editor or an ingest source upgrades them later.
    """
    known_caps = {c.key: c for c in Capability.objects.all()}
    for cap_key, cap_obj in known_caps.items():
        value = capabilities.get(cap_key, AppCapability.CapabilityValue.UNKNOWN)
        AppCapability.objects.update_or_create(
            app=app, capability=cap_obj,
            defaults={"value": value},
        )
```

This invariant (every `App` has one `AppCapability` per known `Capability`) lets the capability fasets behave honestly: yes / no / unknown counts always sum to the total card count, and `?capability=write_actions:no` returns exactly what it promises.

### 9.5. Deduplication

`find_soft_duplicate(draft)` checks:

```python
def find_soft_duplicate(draft: AppDraft) -> App | None:
    # 1. Exact developer_url + fuzzy name
    if draft.developer_url:
        for cand in App.objects.filter(developer_url__iexact=draft.developer_url):
            if Levenshtein.distance(cand.name.lower(), draft.name.lower()) <= 3:
                return cand
    # 2. Exact install_url
    if draft.install_url:
        cand = App.objects.filter(install_url__iexact=draft.install_url).first()
        if cand: return cand
    # 3. Exact slug
    cand = App.objects.filter(slug=slugify(draft.slug_hint)[:200]).first()
    return cand
```

Soft duplicates are surfaced in admin via a `possible_duplicates` admin filter; merging is manual.

---

## 10. Submissions & Claims (no accounts)

### 10.1. Form (django.forms.Form)

```python
class SubmissionForm(forms.ModelForm):
    class Meta:
        model = Submission
        fields = [
            "app_name", "app_url", "developer_name", "listing_type_hint",
            "platforms_hint", "short_description", "long_description",
            "use_cases", "capabilities", "pricing_model", "launch_status",
            "repo_url", "submitter_email",
        ]
        widgets = {
            "platforms_hint": forms.CheckboxSelectMultiple(choices=PLATFORM_CHOICES),
            "use_cases": TagInputWidget(),
            "capabilities": CapabilityMatrixWidget(),
        }

    def clean_app_url(self):
        url = self.cleaned_data["app_url"]
        if Submission.objects.filter(app_url=url,
                                     status=Submission.Status.PENDING).exists():
            raise forms.ValidationError("This URL is already pending review.")
        return url
```

### 10.2. Rate limiting

Using `django-ratelimit`:

```python
@ratelimit(key="ip", rate="5/d", block=False)
@ratelimit(key="post:submitter_email", rate="3/d", block=False)
def submit(request):
    if getattr(request, "limited", False):
        return render(request, "submissions/rate_limited.html", status=429)
    ...
```

### 10.3. Notification email

```python
@shared_task
def send_submission_notification(submission_id: int):
    s = Submission.objects.get(pk=submission_id)
    body = render_to_string("submissions/email_editor_notification.txt", {"s": s})
    send_mail(
        subject=f"[Submission] {s.app_name}",
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=settings.SUBMISSIONS_NOTIFY_EMAILS,
    )
```

### 10.4. Claim verification

Three proof types in MVP:

1. **`domain_email`**: claimer’s email matches the domain of `App.developer_url`. Auto-checked, then human-confirmed.
2. **`dns_txt`**: claimer adds `_llmappmarket-verify` TXT record to their domain with `proof_token`. Verified by Celery task.
3. **`repo_readme`**: claimer adds line `"LLM App Market verification: <token>"` to the linked repo README. Verified by fetch + grep.

Verification flow is **split in two**: a Celery task performs the automated probe and stores its result; the editor flips the claim into `verified` from Django Admin.

```python
PROBES = {
    "domain_email": verify_domain_email,
    "dns_txt": verify_dns_txt,
    "repo_readme": verify_repo_readme,
}


@shared_task
def run_claim_auto_check(claim_id: int) -> None:
    """Only updates `auto_check_status` + `auto_check_log`.

    Does NOT touch `claim.status` or `App.developer_claim_status`. The
    editor double-checks in admin (the log is shown next to the action
    button) and approves manually.
    """
    claim = ClaimRequest.objects.select_for_update().get(pk=claim_id)
    probe = PROBES.get(claim.proof_type)
    if probe is None:
        outcome, log = ClaimRequest.AutoCheckStatus.INCONCLUSIVE, "unknown proof_type"
    else:
        try:
            ok, log = probe(claim)
            outcome = (ClaimRequest.AutoCheckStatus.PASSED if ok
                       else ClaimRequest.AutoCheckStatus.FAILED)
        except Exception as e:
            outcome, log = ClaimRequest.AutoCheckStatus.INCONCLUSIVE, repr(e)

    claim.auto_check_status = outcome
    claim.auto_check_log = log[:8000]
    claim.auto_check_at = timezone.now()
    claim.save(update_fields=["auto_check_status", "auto_check_log", "auto_check_at"])


def approve_claim(claim: ClaimRequest, editor) -> None:
    """Called from Django Admin (action). Editor is the source of truth."""
    if claim.status == ClaimRequest.Status.VERIFIED:
        return  # idempotent
    claim.status = ClaimRequest.Status.VERIFIED
    claim.decided_at = timezone.now()
    claim.decided_by = editor
    claim.save(update_fields=["status", "decided_at", "decided_by"])

    app = claim.app
    # IMPORTANT: a verified claim flips ONLY developer_claim_status.
    # It does NOT change platform_verification_status (claim != platform listing)
    # and it does NOT mark editorial review.
    app.developer_claim_status = App.DeveloperClaimStatus.CLAIMED
    app.contact_email = claim.claimer_email
    app.save(update_fields=["developer_claim_status", "contact_email"])

    recalc_quality_score(app)
    send_claim_verified_email.delay(claim.pk)
```

The form submission triggers the task once: `run_claim_auto_check.delay(claim.pk)`. The result lands in `auto_check_log` and is rendered next to the *Approve claim* button in the admin. Editor reads the log → decides. No auto-promotion path exists in MVP.

---

## 11. SEO infrastructure

### 11.1. Sitemaps

```python
# apps/seo/sitemaps.py
class AppSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return App.published.all().order_by("-quality_score")

    def lastmod(self, obj):
        return obj.updated_at


class PlatformSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.7
    def items(self): return Platform.objects.all()
    def location(self, p): return f"/{p.public_path}/"  # never derive from slug


class CategorySitemap(Sitemap):
    changefreq = "daily"
    priority = 0.6
    def items(self): return Category.objects.all()
    def location(self, c): return f"/apps/{c.slug}/"


class EditorialSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.5
    def items(self): return Post.objects.filter(is_published=True)
```

Registered in `urls.py`:

```python
sitemaps = {
    "apps":       AppSitemap,
    "platforms":  PlatformSitemap,
    "categories": CategorySitemap,
    "editorial":  EditorialSitemap,
}
urlpatterns += [
    path("sitemap.xml", sitemap_index, {"sitemaps": sitemaps}),
    path("sitemap-<section>.xml", sitemap_section, {"sitemaps": sitemaps},
         name="django.contrib.sitemaps.views.sitemap"),
]
```

### 11.2. Structured data (JSON-LD)

`apps/seo/structured_data.py`:

```python
def app_jsonld(app: App, request) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": app.name,
        "description": app.short_description,
        "applicationCategory": ", ".join(c.name for c in app.categories.all()),
        "operatingSystem": ", ".join(p.name for p in app.platforms.all()),
        "url": request.build_absolute_uri(app.get_absolute_url()),
        "image": request.build_absolute_uri(app.logo.url) if app.logo else None,
        "offers": {
            "@type": "Offer",
            "price": "0" if app.pricing_model == "free" else None,
            "priceCurrency": "USD",
        } if app.pricing_model in {"free", "freemium"} else None,
    }
```

Templates use `{% spaceless %}{{ jsonld|safe }}{% endspaceless %}` inside a `<script type="application/ld+json">` block.

### 11.3. Canonical & OG

Every page template extends `base.html` which renders:

```html
<link rel="canonical" href="{% canonical %}">
<meta property="og:title" content="{% block og_title %}{{ block.super }}{% endblock %}">
<meta property="og:description" content="...">
<meta property="og:image" content="...">
<meta name="twitter:card" content="summary_large_image">
```

`{% canonical %}` is a custom template tag that builds the canonical URL from the current view’s primary identifier (slug), stripping query params.

### 11.4. robots.txt

```python
def robots(request):
    body = """\
User-agent: *
Allow: /
Disallow: /admin/
Disallow: /go/
Sitemap: https://llmappmarket.com/sitemap.xml
"""
    return HttpResponse(body, content_type="text/plain")
```

---

## 12. Background tasks

All tasks live under `apps/<module>/tasks.py`. Celery beat schedule (in settings):

```python
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
```

Key tasks (signatures only — full implementations follow the patterns above):

```python
@shared_task
def refresh_search_vector_task(app_id: int) -> None: ...

@shared_task
def refresh_search_vectors_batch() -> None:
    """Safety net: walk all rows once a day."""

@shared_task
def check_app_links_batch() -> None:
    """Pick 5% of apps with oldest last_checked_at and enqueue HEAD probes."""

@shared_task
def check_single_app_link(app_id: int) -> None:
    """For each target URL (official, install, directory, repo):
      1. HEAD request with timeout=10s.
      2. Insert a LinkCheckResult row.
      3. Upsert LinkHealth for (app, target):
           - if ok: consecutive_failures=0, last_ok_at=now
           - if not ok: consecutive_failures += 1, last_failed_at=now
      4. If any LinkHealth.consecutive_failures >= 7:
           - set App.launch_status = DEPRECATED
           - notify editor (admin queue + email)
    """
    ...

@shared_task
def rebuild_sitemap() -> None:
    """Invalidate cached sitemap and prewarm /sitemap.xml."""

@shared_task
def send_submission_notification(submission_id: int) -> None: ...

@shared_task
def send_claim_verified_email(claim_id: int) -> None: ...

@shared_task
def create_weekly_draft() -> None:
    """Create a draft Issue prefilled with new apps from the past 7 days."""

@shared_task
def send_issue(issue_id: int) -> None: ...
```

Signal wiring in `apps/catalog/signals.py`:

```python
from django.db import transaction
from django.db.models.signals import post_save, m2m_changed
from django.dispatch import receiver
from apps.search.tasks import refresh_search_vector_task
from .models import App


@receiver(post_save, sender=App)
def on_app_saved(sender, instance, **kwargs):
    transaction.on_commit(
        lambda: refresh_search_vector_task.delay(instance.pk)
    )


@receiver(m2m_changed, sender=App.categories.through)
@receiver(m2m_changed, sender=App.platforms.through)
@receiver(m2m_changed, sender=App.use_cases.through)
@receiver(m2m_changed, sender=App.listing_types.through)
def on_app_m2m_changed(sender, instance, action, **kwargs):
    if action in {"post_add", "post_remove", "post_clear"}:
        transaction.on_commit(
            lambda: refresh_search_vector_task.delay(instance.pk)
        )


# AppCapability is a through model with its own row lifecycle.
@receiver(post_save, sender="catalog.AppCapability")
def on_app_capability_saved(sender, instance, **kwargs):
    transaction.on_commit(
        lambda: refresh_search_vector_task.delay(instance.app_id)
    )
```

---

## 13. Caching

Goal: serve the homepage and platform/category pages in single-digit ms once warm. We never cache personalized content (there is none).

### 13.1. Page cache

```python
@cache_page(60 * 15, key_prefix="home_v1")
def home(request): ...

@cache_page(60 * 30, key_prefix="platform_v1")
def platform_page(request, platform_slug): ...

@cache_page(60 * 30, key_prefix="category_v1")
def category_page(request, category_slug): ...
```

Catalog list and app detail are **not** cached at the page level — too many parameter combinations. Instead, fragment-cache subcomponents:

```django
{% cache 300 trending_block %}
  {% include "partials/trending.html" %}
{% endcache %}

{% cache 600 newly_added_block %}
  {% include "partials/newly_added.html" %}
{% endcache %}
```

### 13.2. Invalidation

When an editor publishes an App or changes featured flags:

```python
from django.core.cache import cache

def invalidate_listing_caches():
    cache.delete_many([
        "views.decorators.cache.cache_page.home_v1",
        # platform_v1, category_v1: use a pattern delete via Redis directly
    ])
```

For Redis-backed caches we use the `django-redis` `delete_pattern("platform_v1*")` helper.

### 13.3. Outbound redirect is not cached

`/go/...` is always a fresh DB write (`ClickEvent`). It must remain uncacheable.

---

## 14. Admin

### 14.1. App admin

```python
@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "platform_verification_status",
                    "editorial_review_status", "developer_claim_status",
                    "quality_score", "is_featured", "last_checked_at")
    list_filter = ("status", "platform_verification_status",
                   "editorial_review_status", "developer_claim_status",
                   "launch_status", "pricing_model", "is_featured")
    search_fields = ("name", "slug", "developer_name", "short_description")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [AppPlatformInline, AppCategoryInline, AppCapabilityInline,
               AppUseCaseInline, SourceInline]
    actions = ["action_publish", "action_mark_editorial_reviewed",
               "action_mark_platform_official",
               "action_recalculate_quality", "action_refresh_search_vector"]
    readonly_fields = ("first_seen_at", "last_checked_at", "quality_score",
                       "created_at", "updated_at")

    @admin.action(description="Publish (with validation)")
    def action_publish(self, request, qs):
        ok, fail = 0, []
        for app in qs:
            try:
                transition_to_published(app, request.user); ok += 1
            except ValueError as e:
                fail.append(f"{app.name}: {e}")
        if fail: self.message_user(request, "; ".join(fail), level=messages.WARNING)
        self.message_user(request, f"Published {ok}")

    @admin.action(description="Mark as editorially reviewed")
    def action_mark_editorial_reviewed(self, request, qs):
        qs.update(editorial_review_status=App.EditorialReviewStatus.REVIEWED,
                  last_checked_at=timezone.now())

    @admin.action(description="Mark as listed in official platform directory")
    def action_mark_platform_official(self, request, qs):
        """Only acts on rows that have at least one AppPlatform with an
        official_directory_url filled in — guard against bare flags."""
        ok = qs.filter(platform_links__official_directory_url__gt="").distinct()
        ok.update(
            platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
            last_checked_at=timezone.now(),
        )
        skipped = qs.exclude(pk__in=ok.values("pk")).count()
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} app(s) without an official_directory_url.",
                level=messages.WARNING,
            )

    @admin.action(description="Recalculate quality score")
    def action_recalculate_quality(self, request, qs):
        for app in qs: recalc_quality_score(app)

    @admin.action(description="Refresh search vector")
    def action_refresh_search_vector(self, request, qs):
        for app in qs: refresh_search_vector_task.delay(app.pk)
```

### 14.2. Moderation queues

Each of `Submission`, `ClaimRequest`, and `App` (drafts) gets a saved view:

- `/admin/submissions/submission/?status__exact=pending`
- `/admin/submissions/claimrequest/?status__exact=pending`
- `/admin/catalog/app/?status__exact=draft`

A small custom admin index template highlights these counts at the top of `/admin/`.

### 14.3. Pre-publish checklist widget

The `App` change form renders a sidebar showing red/green checks for each criterion in `transition_to_published`. Editors see at a glance what is missing before clicking *Publish*.

---

## 15. Observability & ops

### 15.1. Sentry

Standard `sentry-sdk[django,celery]` setup. Two DSNs (web + worker) feeding the same project; release tied to git SHA.

### 15.2. Structured logging

```python
LOGGING = {
    "version": 1,
    "formatters": {
        "json": {"()": "pythonjsonlogger.jsonlogger.JsonFormatter"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
```

Every request logs `request_id` (UUID per request) via middleware; Celery tasks inherit `request_id` from headers when called as `task.apply_async(headers={"request_id": ...})`.

### 15.3. Healthcheck

```python
def healthcheck(request):
    checks = {
        "db": _check_db(),
        "redis": _check_redis(),
        "pg_trgm": _check_pg_trgm(),  # extension is loaded
    }
    status = 200 if all(checks.values()) else 503
    return JsonResponse({"status": "ok" if status == 200 else "fail", "checks": checks},
                        status=status)
```

`_check_pg_trgm` issues `SELECT 'a' % 'a'` and returns `False` if Postgres replies with "operator does not exist" — a clear signal that the extension is missing from the running DB.

Hooked at `/healthz/` and used by load balancer + uptime monitor.

### 15.4. Seed fixtures

Located at `apps/catalog/fixtures/seed.json`. Contains:

- 5 `Platform` records
- 6 `ListingType` records
- 10 `Category` records (the 10 from `business.md` § 6.2)
- 8 `Capability` records (the 8 from `business.md` § 6.3)

Loaded automatically on first `make seed`. Subsequent runs are idempotent (`update_or_create`).

---

## 16. Local dev & deploy

### 16.1. Docker compose

```yaml
services:
  postgres:
    image: postgres:16
    environment: { POSTGRES_DB: llmmarket, POSTGRES_USER: llmmarket, POSTGRES_PASSWORD: llmmarket }
    volumes: [pgdata:/var/lib/postgresql/data]
  redis:
    image: redis:7
  web:
    build: ./docker
    command: gunicorn config.wsgi:application -b 0.0.0.0:8000
    env_file: .env
    depends_on: [postgres, redis]
    ports: ["8000:8000"]
  worker:
    build: ./docker
    command: celery -A config worker -l info
    env_file: .env
    depends_on: [postgres, redis]
  beat:
    build: ./docker
    command: celery -A config beat -l info
    env_file: .env
    depends_on: [postgres, redis]
volumes: { pgdata: {} }
```

### 16.2. Makefile

```makefile
seed:        ; python manage.py loaddata apps/catalog/fixtures/seed.json
migrate:     ; python manage.py migrate
runserver:   ; python manage.py runserver
ingest:      ; python manage.py shell -c "from apps.sources.tasks import ingest_mcp_registry; ingest_mcp_registry()"
refresh:     ; python manage.py shell -c "from apps.search.tasks import refresh_search_vectors_batch; refresh_search_vectors_batch()"
tailwind:    ; npx tailwindcss -i static/src/app.css -o static/dist/app.css --watch
test:        ; pytest -q
```

### 16.3. Production deploy

Single Docker image. Three runtime containers (web / worker / beat) share it.

1. Build image with git SHA tag.
2. Apply migrations (`python manage.py migrate`) as a one-shot job before flipping traffic.
3. Run `python manage.py collectstatic --noinput` during build.
4. Health check before swap.
5. After deploy: run `refresh_search_vectors_batch` once if any field contributing to `search_vector` was added/changed; otherwise skip (signals keep it warm).

### 16.4. Secrets & config

All via environment variables (12-factor). Required at minimum:

```
DATABASE_URL
REDIS_URL
DEFAULT_FROM_EMAIL, EMAIL_BACKEND_URL
SUBMISSIONS_NOTIFY_EMAILS=editor@llmappmarket.com,...
TURNSTILE_SITE_KEY, TURNSTILE_SECRET_KEY
SENTRY_DSN
SECRET_KEY
ALLOWED_HOSTS=llmappmarket.com
```

Never check in real values; provide `.env.example`.

---

## Appendix A — what the implementer should do first

A pragmatic 1–2 day bootstrap order, matching Phase 1 (Public Alpha) of the roadmap in `business.md` § 17:

1. Init Django 5 project, set up settings split (`base/dev/prod`).
2. Add `core` and `catalog` apps; ship the models from § 5.1–5.4 (including `Platform.public_path`, three trust statuses on `App`, extended `AppPlatform`, `LinkCheckResult` / `LinkHealth`); create an early migration that enables `pg_trgm` (`TrigramExtension`); run migrations.
3. Add fixtures (seed): 5 `Platform` records with `public_path`, 6 `ListingType`, 10 `Category`, 8 `Capability`. `make seed`, verify Django admin shows everything.
4. Wire `submissions` and `claims` skeletons (forms + admin only). For claims: ship `auto_check_status` field and `run_claim_auto_check` task, but **no auto-promotion** — admin action `approve_claim` is the only path to verified.
5. Implement `apps/search/vector.py` (`SEARCH_VECTOR_EXPR`, two-step `refresh_search_vector`) + the signals in § 12; verify `App.search_vector` is populated by saving a draft in admin.
6. Implement `apps/search/views.py` + `filters.py` (capability key:value + region) + `facets.py`; build `catalog/list.html` + `partials/catalog_results.html` + the HTMX faceted search.
7. Build `catalog/detail.html` with three independent trust badges and JSON-LD.
8. Add `MCPRegistrySource` + `ingest_mcp_registry` task with retries, schema-version tracking, and an `UnparsedRegistryRecord` queue; run it once manually; moderate the drafts.
9. Add sitemaps + robots + canonical helper. Platform sitemap MUST read from `Platform.public_path` — never auto-derive from slug.
10. Add outbound redirect view, `LinkCheckResult` + `LinkHealth`, and the `check_single_app_link` task that flips `launch_status=DEPRECATED` after 7 consecutive failures.

Everything else (editorial pages, newsletter, link checker, advanced caching) layers on top once the above loop is closed.
