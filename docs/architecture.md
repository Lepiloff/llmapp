# LLM App Market — backend architecture

> Technical companion to [`business.md`](./business.md).
> Last updated: 2026-05-23.

This is the compact architecture reference for engineers and agents changing
the codebase. It records the system boundaries and invariants that should not
be rediscovered from scratch. Historical plans, rollout logs, and long code
sketches live in git history.

---

## Table of contents

1. [Tech stack](#1-tech-stack)
2. [High-level architecture](#2-high-level-architecture)
3. [Project layout](#3-project-layout)
4. [Data model](#4-data-model)
5. [Model and service contracts](#5-model-and-service-contracts)
6. [Search layer](#6-search-layer)
7. [HTMX and UI patterns](#7-htmx-and-ui-patterns)
8. [Routing and public API](#8-routing-and-public-api)
9. [Sources and ingest](#9-sources-and-ingest)
10. [Editorial content, submissions, and claims](#10-editorial-content-submissions-and-claims)
11. [SEO, newsletter, and link health](#11-seo-newsletter-and-link-health)
12. [Background tasks](#12-background-tasks)
13. [Caching](#13-caching)
14. [Admin](#14-admin)
15. [Observability and ops](#15-observability-and-ops)
16. [Local dev and deploy](#16-local-dev-and-deploy)

---

## 1. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Runtime | Python 3.12 | Single backend runtime. |
| Web | Django 5.x | ORM, admin, forms, sitemaps, security. |
| DB/search | PostgreSQL 16 | Source of truth, FTS, trigram search, facets. |
| Queue | Celery 5 | Worker + beat for ingest, checks, cleanup. |
| Broker/cache | Redis 7 | Celery broker and Django cache. |
| Frontend | Django templates + HTMX | SSR-first, SEO-friendly, minimal JS. |
| Assets | Local media/S3-compatible later | Logos, screenshots, OG images. |
| Monitoring | Sentry + structured logs | Optional DSN, request IDs in logs. |
| Deploy | Docker Compose | One web, worker, beat, Postgres, Redis. |

Out of scope for MVP: separate frontend app, user accounts, dedicated search
engine, automatic publication, and marketplace payments.

Postgres remains the search engine until catalog size or query behavior proves
otherwise. Any future search-engine swap should be isolated inside
`apps/search/`.

---

## 2. High-level architecture

```
Browser
  |
  | HTML / HTMX partials
  v
Django web
  |-- catalog/search/views/forms/admin
  |-- submissions/claims/editorial/newsletter/seo
  |-- sources + agent pipeline entrypoints
  |
  v
PostgreSQL <---- Celery worker/beat ---- external sources
  |                                      - MCP Registry
  |                                      - GitHub/RSS
  |                                      - Gemini JSON
  |                                      - Claude/ChatGPT HTML sources
  v
Redis cache/broker
```

Core ownership boundaries:

- `apps/catalog` owns public `App` data and editorial lifecycle.
- `apps/sources` owns external-source records, ingest normalization, link health,
  duplicate candidates, and `upsert_app_from_draft()`.
- `apps/agent` owns LLM orchestration, enrichment, budget, review queue, and
  source-discovery tasks that use the same source/upsert contract.
- `apps/search` owns FTS/trigram query behavior and search analytics.
- `apps/seo`, `apps/newsletter`, `apps/editorial`, `apps/submissions`, and
  `apps/analytics` are feature apps layered around catalog data.

Hard public-data invariant: only editor-controlled flows publish listings.
Automated source discovery creates or updates `DRAFT`/`UNREVIEWED` cards and
records evidence for review.

---

## 3. Project layout

Important directories:

- `config/` — Django settings, URLs, WSGI/ASGI.
- `apps/catalog/` — core taxonomy, `App`, `AppPlatform`, quality score,
  publish transitions, public catalog views.
- `apps/sources/` — source models, `AppDraft`, MCP Registry source, upsert,
  link checker, duplicate candidates.
- `apps/agent/` — LLM client, pure pipeline code, persistence bridge, tasks,
  admin dashboards.
- `apps/search/` — query parsing, FTS/trigram search, facets, search logs.
- `apps/submissions/` — public submit/claim flows.
- `apps/editorial/` — blog posts, comparisons, collections.
- `apps/newsletter/` — subscribers, issues, open/click tracking.
- `apps/seo/` — sitemap, metadata, structured data.
- `apps/core/` — healthcheck, middleware, context processors.
- `docs/` — canonical project docs.
- `templates/`, `static/`, `media/` — server-rendered UI and assets.

Rule of thumb: do not put source-specific parsing in `catalog`, and do not put
catalog mutation logic directly in source connectors.

---

## 4. Data model

Primary catalog entities:

- `App` — one catalog listing. It may represent a ChatGPT App, Claude Connector,
  Gemini App/Extension, MCP Server, Enterprise Agent, or a multi-platform item.
- `Platform` — ecosystem/client (`chatgpt`, `claude`, `gemini`, `mcp`,
  `enterprise`) with `public_path` for platform pages.
- `ListingType` — class of listing (`chatgpt-app`, `claude-connector`,
  `interactive-claude-app`, `mcp-server`, `gemini-app`,
  `gemini-extension`, `enterprise-agent`).
- `Category`, `Capability`, `UseCase` — taxonomy and search/facet surface.
- `AppPlatform`, `AppCategory`, `AppCapability`, `AppUseCase` — through tables.

Source and operations entities:

- `Source` — where a listing came from. A single `App` can have multiple
  sources. Unique constraint: `(source_type, external_id)` when `external_id`
  is not empty.
- `DuplicateCandidate` — weak duplicate signal that needs editor review.
- `UnparsedRegistryRecord` — raw MCP Registry rows that could not be normalized.
- `LinkCheckResult`, `LinkHealth` — link-probe audit trail and rolling summary.
- Agent tables: `AgentRun`, `EnrichmentTask`, `LLMCallLog`,
  `NeedsReviewQueueEntry`, `BudgetMonthState`.

The trust model is split into three independent axes:

- `platform_verification_status` — whether a platform/source lists the app.
- `editorial_review_status` — whether our editor reviewed it.
- `developer_claim_status` — whether a developer claim is verified.

Do not collapse those into one badge or one status field. Public visibility is
`App.status`; product availability is `App.launch_status`.

---

## 5. Model and service contracts

### 5.1 `App`

`App.status` is the editorial visibility lifecycle:

- `draft` — visible only in admin/review flows.
- `published` — visible publicly.
- `hidden` — deliberately removed from public pages.

Automated ingest and LLM code must create drafts, not published listings.

### 5.2 `Source`

`Source` records external provenance. Important fields:

- `source_type` — enum covering manual, submission, MCP Registry, GitHub MCP,
  RSS, Gemini Extensions, Claude Connectors, ChatGPT unofficial discovery, and
  agent enrichment.
- `external_id` — upstream stable identity when available.
- `source_url` — source page/API URL.
- `payload` — raw payload plus enrichment metadata/evidence.
- `is_primary`, `is_active`, `fetched_at`, `last_enriched_at`.

The uniqueness constraint on `(source_type, external_id)` is the first line of
defense against repeated ingest of the same upstream item.

### 5.3 Submissions and claims

`Submission` and `ClaimRequest` are public input queues, not publication paths.
They can create reviewable records, but an editor still decides publication and
developer-claim status.

### 5.4 Link health

`LinkCheckResult` is append-only probe history. `LinkHealth` is the current
rolling summary per `(app, target, url)` and tracks `consecutive_failures`,
`last_ok_at`, and `last_failed_at`.

Auto-deprecation is based on repeated failures, not one bad HTTP response.
Visibility still remains an editor decision.

### 5.5 Duplicate candidates

`DuplicateCandidate` stores weak duplicate evidence:

- `app` — newly created draft.
- `candidate_app` — existing app that may be the same product.
- `source` — source that introduced the new draft.
- `match_reason`, `score`, `evidence`, `status`, timestamps.

Editors resolve candidates as confirmed or dismissed in admin. Confirming a
candidate is an editorial decision; the system does not silently merge weak
signals.

### 5.6 Services

`apps.catalog.services` owns editorial transitions and quality scoring:

- `transition_to_published()` — controlled publish path.
- `recalc_quality_score()` — internal ordering signal.
- use-case merge helpers — de-duplicate taxonomy labels safely.

Source and agent code must not bypass these services for editor-owned state.

---

## 6. Search layer

Search is PostgreSQL-backed:

- `App.search_vector` is materialized and indexed.
- Trigram indexes support typo-tolerant fallback.
- Taxonomy text is included in `search_index_text`/vector refresh so platform,
  category, and use-case names can match queries.
- Facets are computed through ORM aggregations over the current filtered result.

`apps/search/` is the boundary for search behavior:

- `views.py` owns catalog search page behavior.
- `tasks.py::refresh_app_search_vector` updates vectors.
- search logs feed suggestions/popular searches.

Signals refresh search data when relevant catalog objects change. A scheduled
task can rebuild vectors as a safety net.

### 6.1 Query behavior

Catalog queries combine FTS, trigram fallback, filters, ordering, and facet
aggregation inside `apps/search/`. Public views should call that layer instead
of hand-building search SQL.

### 6.2 Search vector refresh

`App.search_vector` is refreshed from app fields plus taxonomy names. Signals
cover normal writes; the scheduled refresh task is the repair path for missed
events or bulk operations.

---

## 7. HTMX and UI patterns

The public UI is server-rendered first. HTMX enhances filtering, pagination,
and form interactions without making the site depend on a client-side SPA.

Patterns:

- Full page works without JS.
- HTMX requests return partial templates for catalog result lists/facets.
- Form validation lives in Django forms.
- Avoid duplicating business rules in JavaScript.
- Public pages should remain crawlable HTML.

Templates should reflect the domain: a dense catalog and review workflow, not a
marketing-only landing page.

---

## 8. Routing and public API

Public routes:

- `/` — homepage.
- `/apps/` — searchable catalog.
- `/apps/<slug>/` — app detail.
- `/apps/<category>/` — category page.
- `/<platform_public_path>/` — platform page, e.g. `/chatgpt-apps/`.
- `/<platform_public_path>/<category>/` — cross-page.
- `/submit/`, `/claim/` — public input flows.
- `/go/<app_slug>/...` — outbound redirect/click tracking.
- `/health/` — operational healthcheck.
- SEO endpoints: sitemap and robots.

Admin routes are Django admin routes. Public API endpoints should stay small and
read-only unless there is a product reason to expand them.

---

## 9. Sources and ingest

### 9.1 Source interface

Every source emits `AppDraft` records via `BaseSource.iter_drafts()`.
`AppDraft` is the normalized in-memory contract. Source connectors should not
write ORM rows directly.

Implemented/active sources:

- `MCPRegistrySource` — official MCP Registry REST API (`/v0`).
- `GitHubMCPSearchSource` — GitHub MCP discovery.
- `RSSFeedSource` — vendor/blog/topic RSS discovery.
- `GeminiExtensionsSource` — Gemini Extensions JSON ingest.
- `ClaudeConnectorsSource` — conservative HTML crawl with robots enforcement.
- `ChatGPTAppsSource` — unofficial crawlable `mcpapp.net/chatgpt-apps` index.

### 9.2 MCP Registry source

MCP Registry is preview status. The source treats upstream shapes as untrusted:
malformed rows go to `UnparsedRegistryRecord`, and one bad row must not crash
the whole batch. The default endpoint is `/v0`; old `/v1/servers` returns 404.

MCP Registry listings are platform-official for MCP, but still
`editorial_review_status=unreviewed` until an editor reviews them.

### 9.3 Upsert path

All new/updated source drafts go through:

```text
BaseSource.iter_drafts() -> AppDraft -> upsert_app_from_draft()
```

`upsert_app_from_draft()` is responsible for:

- idempotence by `Source(source_type, external_id)`;
- duplicate detection before new `App` creation;
- creating `App` as `draft`;
- creating/refreshing `Source`;
- attaching platforms, listing types, categories, capabilities, and use cases;
- preserving editor-owned fields on refresh.

### 9.4 Attach helpers

Attach helpers only fill missing/unknown relation data. For `AppPlatform`,
metadata is shallow-merged with editor edits winning conflicts. This protects
manual review work from repeated discovery cycles.

### 9.5 Deduplication

Duplicate handling has two paths:

1. **Strong identity match -> no new `App`.** `find_soft_duplicate()` returns
   the existing app when there is a strong signal: normalized exact URL, GitHub
   `owner/repo`, developer domain plus sufficiently similar name, or exact slug.
   The new source is attached as another `Source` row.
2. **Weak signal -> `DuplicateCandidate`.** `find_duplicate_candidates()`
   records shared-domain/similar-name or very-similar-name evidence for editor
   review. It does not silently merge.

LLM-generated descriptions are not identity signals by themselves. Similar text
can support review but cannot auto-merge two projects without URL/name/domain
evidence.

---

## 10. Editorial content, submissions, and claims

Editorial features:

- blog posts;
- app collections;
- app comparisons;
- editorial picks and digest inputs.

Submission flow:

- public user submits an app/source;
- record enters pending/moderation state;
- editor converts or links it to an `App`.

Claim flow:

- developer submits claim evidence;
- editor verifies domain/contact proof;
- `developer_claim_status` changes only through review.

No public input flow publishes a listing directly.

---

## 11. SEO, newsletter, and link health

SEO:

- Sitemaps are generated from published public pages.
- Structured data lives in `apps/seo/structured_data.py`.
- Canonical URLs come from current routing.
- Sitemap cache must be invalidated when published app visibility/content changes.

Newsletter:

- Issues reference apps through join tables.
- Open/click tracking is stored separately from catalog state.
- Email generation should not mutate app editorial fields.

Link health:

- Daily link checks update `LinkHealth` and record `LinkCheckResult`.
- After 7 consecutive failures, `launch_status` may become `deprecated`.
- `App.status` is never hidden automatically.

---

## 12. Background tasks

### 12.1 Task families

Beat/worker tasks cover:

- source ingest (`ingest_mcp_registry`, Gemini, Claude, ChatGPT);
- discovery (`discover_github_mcp`, `discover_rss`);
- enrichment and review queue generation;
- re-actualization and vanish detection;
- link checking;
- search vector refresh;
- sitemap rebuild;
- popular/trending recalculation;
- newsletter drafts;
- old audit/log cleanup.

Tasks should be idempotent and safe to retry. One bad external row should not
abort a whole source batch.

### 12.2 Search refresh

`apps/search/tasks.py::refresh_app_search_vector` is the canonical single-app
refresh entrypoint. Batch jobs should call that behavior instead of duplicating
vector composition logic.

---

## 13. Caching

Redis is the cache backend. Cache rules:

- Public page fragments may be cached.
- Sitemap cache has explicit invalidation/fallback clear.
- Outbound redirects should not be cached in a way that drops analytics.
- Admin/review pages should prefer fresh DB state.

Code that invalidates cache should tolerate Redis outages; Redis is an
accelerator, not source of truth.

---

## 14. Admin

Django admin is the primary operations UI for MVP:

- app review and publish decisions;
- source inspection;
- duplicate candidates;
- unparsed registry records;
- link-health audit;
- submissions and claims;
- agent run/cost dashboards;
- needs-review queue;
- SEO/editorial/newsletter management.

Admin actions must preserve invariants: no automated publish by LLM/source
tasks, no silent overwrite of editor-owned fields, and manual resolution for
weak duplicate candidates.

---

## 15. Observability and ops

### 15.1 Sentry

Sentry is optional by env but production should set it. External-source failures
should report useful context without leaking secrets.

### 15.2 Structured logging

`RequestIDMiddleware` attaches request IDs so web logs can be correlated.
Background tasks should log source type, external ID, counts, and failure
reasons.

### 15.3 Healthcheck

`/health/` checks DB, Redis, `pg_trgm`, and Celery worker responsiveness. In
production, failure should surface as non-200 so monitoring catches it.

### 15.4 Seed fixtures

Reference data seeds platforms, listing types, categories, capabilities, and
other small lookup tables. Demo apps are not the canonical content source now
that discovery produces real drafts.

---

## 16. Local dev and deploy

Local:

- use Docker Compose for Postgres/Redis/web/worker/beat;
- host access to Postgres is `127.0.0.1:5432`;
- inside compose, services use `postgres:5432` and `redis:6379`;
- migrations run through the entrypoint and can be run manually with
  `python manage.py migrate`.

Production:

- `DJANGO_SETTINGS_MODULE=config.settings.prod`;
- `SECRET_KEY`, `ALLOWED_HOSTS`, `SITE_BASE_URL`, `DATABASE_URL`, Redis, email,
  Sentry, and source flags come from env;
- `DEBUG=False`;
- `web`, `worker`, and `beat` must all be running for discovery and review
  workflows to stay fresh.

Before enabling production discovery, use `docs/pre-launch-checklist.md` for
the current rollout order and non-code blockers.
