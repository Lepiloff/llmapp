# Agent Pipeline — Rollout Log

Condensed status + history of the semi-autonomous LLM catalog pipeline
specified in `docs/agent-pipeline.md`. Old phases are summarised in one
paragraph with commit hashes; recent Phase 3 scale-up keeps full detail.

For forensic depth on any compressed entry, run `git log --follow` on
the file it points to. The `Append-only` convention from earlier
revisions is dropped in favour of keeping this doc usable as a session
bootstrap.

---

## Status snapshot — 2026-05-15

* **Phase 0** — MCP Registry ingest stabilized. ✅
* **Phase 1 + 1b** — LLM enrichment for existing DRAFTs, real OpenAI
  provider live. ✅
* **Phase 2** — Admin review queue + bulk publish. ✅
* **Phase 3** — Discovery (RSS + GitHub MCP), new-app enrichment,
  README fetch, manual CLI, gate report. ✅ **Phase 3 → Phase 4 gate
  is OPEN.**
* **Phase 4** — Prereqs done (link-checker + `Source.last_enriched_at`).
  Re-actualization pipeline, official directories, beat schedule still
  to be built.
* **Phase 5** — Not started: budget hard-stop, admin dashboard, eval
  pack.

**Catalog state right now:**

```
Phase 3 -> Phase 4 gate: OPEN
Generated RSS/GitHub apps: 25 (draft=1, published=24, hidden=0)
Approval rate: 96.0%
LLM cost: $0.147591 total; $0.006150 per published app
LLM calls: 25 (real=25, mock=0)
Cost basis complete: yes
```

* Catalog total: **25 apps**, 24 published from real GitHub MCP
  discovery, 1 DRAFT (Trigger.dev — workflow runtime, not a clean
  listing-type fit).
* Demo seed apps removed; reference data (Platform / Category /
  Capability / ListingType from `seed.json`) kept.

**Hard constraints held throughout** (asserts live in code +
`tests/agent/test_persist.py`):

* LLM never touches `App.status`, `editorial_review_status`,
  `platform_verification_status`, `developer_claim_status`,
  `App.verdict`.
* Capability `yes/no` only with evidence (otherwise `unknown`).
* `apply_merge_set` opens `SELECT … FOR UPDATE` and re-checks each
  field against the locked row — editor edits between snapshot and
  apply always win.
* Discovery `--apply` is gated on `AGENT_SOURCES_ENABLED`; dry-run
  bypasses the flag.

---

## Environment + ops runbook

* Provider config (`.env`):
  ```
  AGENT_LLM_PROVIDER_PRIMARY=openai
  AGENT_LLM_MODEL_PRIMARY=gpt-5.4-mini
  AGENT_LLM_PROVIDER_CHEAP=openai
  AGENT_LLM_MODEL_CHEAP=gpt-5.4-nano
  # Per-role + per-channel pricing (input / cached / output, $/1M tokens):
  AGENT_OPENAI_PRIMARY_INPUT_COST_PER_1M_TOKENS=0.75
  AGENT_OPENAI_PRIMARY_CACHED_COST_PER_1M_TOKENS=0.075
  AGENT_OPENAI_PRIMARY_OUTPUT_COST_PER_1M_TOKENS=4.50
  AGENT_OPENAI_CHEAP_INPUT_COST_PER_1M_TOKENS=0.20
  AGENT_OPENAI_CHEAP_CACHED_COST_PER_1M_TOKENS=0.02
  AGENT_OPENAI_CHEAP_OUTPUT_COST_PER_1M_TOKENS=1.25
  AGENT_MONTHLY_BUDGET_USD=20
  AGENT_SOURCES_ENABLED=           # discovery off by default
  GITHUB_TOKEN=<pat>                # 30 req/min search, 5000/h contents
  OPENAI_API_KEY=<sk-...>
  ```
* Anthropic provider stub exists in `apps/agent/llm/client.py:184` but
  raises `NotImplementedError`. **Deferred** until after prod release
  per operator policy.
* Cost backfill: `manage.py agent_backfill_costs` walks every non-mock
  `LLMCallLog`, matches `model` to the configured primary/cheap roles,
  and rewrites `cost_usd`. `--include-nonzero` re-applies prices after
  a vendor price change. Phase 3's 25 discovery rows + 36 prior MCP-
  registry enrichment rows backfilled at total $0.153408 (post-fix
  per-published-app cost: $0.006150).

**Run discovery (preferred path — inside the container, broker
resolves):**

```bash
docker compose exec -T -e AGENT_SOURCES_ENABLED=github_mcp web \
  python manage.py agent_run --source=github_mcp --limit=30 --apply
docker compose exec -T -e AGENT_SOURCES_ENABLED=rss web \
  python manage.py agent_run --source=rss --limit=20 --apply
```

**Run from host venv** (only for dry-run; apply path needs
`CELERY_TASK_ALWAYS_EAGER=True` because the docker hostname `redis`
isn't resolvable from the host):

```bash
DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  DJANGO_SETTINGS_MODULE=config.settings.dev \
  .venv/bin/python manage.py agent_run --source=github_mcp --limit=5
```

**Editor approval pass after a batch:**

```python
# inside docker compose exec -T web python
from apps.catalog.models import App
from apps.catalog.services import transition_to_published
from django.contrib.auth import get_user_model
editor = get_user_model().objects.filter(is_superuser=True).first()
for pk in [...]:
    app = App.objects.get(pk=pk)
    app.editorial_review_status = App.EditorialReviewStatus.REVIEWED
    app.platform_verification_status = App.PlatformVerificationStatus.NOT_LISTED
    app.save(update_fields=["editorial_review_status",
                            "platform_verification_status", "updated_at"])
    transition_to_published(app, editor)
```

**Phase 3 gate snapshot:**

```bash
docker compose exec -T web python manage.py agent_phase3_report
docker compose exec -T web python manage.py agent_phase3_report --json
```

---

## Open findings / known issues

| # | Issue | Where | Impact |
|---|---|---|---|
| F1 | Anthropic provider not implemented | `apps/agent/llm/client.py:184` | Deferred — prod release runs on OpenAI per operator policy |
| F2 | ~~OpenAI per-model cost vars unset~~ | ~~`.env` + `apps/agent/llm/client.py`~~ | **Closed.** Per-role pricing wired with cached-input discount; 61 historical rows backfilled |
| F3 | ~~`/apps/<category-slug>/` returns 404~~ | ~~`apps/catalog/urls.py`~~ | **Closed in `8cc913a`** — single dispatcher view routes detail vs. category by slug lookup |
| F4 | Host → docker-broker route | `redis` hostname unreachable from host venv | `--apply` from host needs `CELERY_TASK_ALWAYS_EAGER=True`; use container path instead |
| F5 | ~~`_enriched_to_app_draft` infers platform from `mcp-server` listing only~~ | ~~`apps/agent/persist.py`~~ | **Closed in `ec249d1`** — `_derive_platforms` covers all 6 listing types |

---

## Phase 0 — MCP Registry ingest stabilized (2026-05-13) ✅

Replaced inline ingest in `apps/sources/tasks.py::ingest_mcp_registry`
with `MCPRegistrySource().iter_drafts()` + `upsert_app_from_draft()`.
Added migration `apps/sources/migrations/0002_backfill_mcp_appplatform`
backfilling `AppPlatform` rows and flipping
`platform_verification_status` to `official` for unreviewed MCP imports.
Post-review hardening added type-checks around `resp.json()` →
non-dict payloads / malformed `next_cursor` / non-dict records route to
`UnparsedRegistryRecord` instead of crashing the task. Regression
coverage in `tests/sources/test_ingest_mcp_registry.py` (5) and
`tests/sources/test_mcp_registry_source.py` (13). Pre-check verified
`pg_trgm`, search-vector signal refresh, URL routes, and
`Platform(slug='mcp')` seed.

Two link-checker bugs were noted as deferred Phase 4 prerequisites:
`last_checked_at IS NULL` excluded never-checked apps, and
auto-deprecate threshold was 5 instead of business-spec 7. Both fixed
in Phase 4 prereq slice (below).

---

## Phase 1 — enrich_existing_draft, mock-only (2026-05-13) ✅

Scaffolded `apps/agent/` Django app with pure-Python core (`llm/`,
`pipeline/`, `sources/`) and Django bridge (`persist.py`). Pydantic v2
schemas (`EnrichedDraft`, `MergeSet`, `AppSnapshot`,
`CapabilityProposal`) with `extra="forbid"`. Validation drops
evidence-less yes/no, unknown slugs, low-confidence categories, and
invalid URLs. Merge policy never overwrites editor text or flips
existing `yes/no` capabilities; editorial fields
(`launch_status` / `pricing_model` / `verdict`) only flow through
`NeedsReviewQueueEntry`. Real-provider stubs raise `NotImplementedError`
until Phase 1b; `MockLLMProvider` drives tests. Management command
`manage.py agent_run --enrich-app=<slug>` / `--enrich-pending`. 75 tests
across `tests/agent/`.

Post-review hardening:

* `AppNotEligibleError` — DRAFT-only gate on the LLM call; container-
  level re-check inside the apply transaction under `SELECT … FOR UPDATE`.
* Race-safe persist: every field/capability update re-checks the
  locked row's current value; editor edits between snapshot and apply
  always win. Regression: `test_field_race_editor_wins_…`,
  `test_capability_race_editor_wins_…`.
* `transaction.on_commit(refresh_search_vector_task.delay)` added on
  `_apply_field_updates` so text-only enrichment doesn't have to wait
  for the nightly rebuild.
* Agent-written audit Sources now use distinct
  `Source.SourceType.AGENT_ENRICH` (migration `sources.0003`).

84 tests green at end of hardening pass.

---

## Phase 1b — Real OpenAI provider (2026-05-13) ✅

`OpenAIProvider` implemented in `apps/agent/llm/client.py:205` using
the Anthropic-style adapter shape over the OpenAI SDK structured-output
endpoint. Provider-specific wire schema added so strict structured
outputs accept the request while the pipeline still consumes the
ergonomic `MergeSet` shape. Cost computed from
`AGENT_OPENAI_*_COST_PER_1M_TOKENS` (see Finding F2). Real-provider
exceptions sanitized before audit logging. `/health/` `pg_trgm` check
fixed to use `similarity('a'::text, 'a'::text)`. Smoke call against
`gpt-5.4-mini` returned 200 with parsed `MergeSet`. 51 tests green.

---

## Phase 2 — Admin review queue (2026-05-13) ✅

`NeedsReviewQueueEntryAdmin` extended from raw-JSON to render the
current App state next to LLM proposals with `evidence_map`, prompt
version, model, `LLMCallLog` link. Object + bulk actions for "Apply
proposed verdict", "Apply launch status", "Apply pricing", "Reject
all", "Mark resolved", "Approve & publish" (via
`apps.catalog.services.transition_to_published`). Resolution metadata
fields (`resolved_at`, `resolved_by`, `resolution_note`,
`review_outcome`) added so Phase 1 acceptance gate #7 can be computed
from DB state. Daily digest task `send_review_queue_digest` wired
to beat at 07:30 UTC. Helper `review_acceptance_stats(days=30)` for
acceptance-rate measurement. 108 tests green at end.

---

## Phase 3 — RSS + GitHub MCP discovery (2026-05-13..14) ✅

Slices 1-5 (condensed):

* `apps.agent.sources.rss_feeds.RSSFeedSource` — stdlib XML parsing of
  RSS 2.0 + Atom (Anthropic / OpenAI / Google AI blogs + GitHub MCP
  topic feed). `apps.agent.sources.github_mcp_search.GitHubMCPSearchSource`
  — GitHub Search + Contents API for README via base64.
* `DiscoveryDecision` schema + `discover-v1.0` cheap prompt for
  YES/NO/canonical URL classification.
* `EnrichedDraft` + `enrich-new-v1.0` primary prompt; pure-Python
  `validate_enriched_draft` mirrors merge-path guardrails.
* `apps.agent.persist.persist_new_draft` is the only Django bridge for
  agent-discovered new cards; goes through `upsert_app_from_draft` so
  DRAFT status / `unreviewed` / verdict-empty invariants are enforced
  by the catalog code, not the agent.
* Celery beat: `discover_rss` every 6 h, `discover_github_mcp` Mon/Wed/
  Fri 06:30 UTC. Both guarded by `AGENT_SOURCES_ENABLED`.
* `apps.agent.reports.phase3_gate_report` + `manage.py
  agent_phase3_report [--json]` — the canonical gate evidence rollup.
  Source rows with `payload.agent_enrichment` are the source of truth
  for "LLM-generated via RSS/GitHub".

Phase 4 prereqs landed in the same window: link-checker selector now
unions `last_checked_at IS NULL`, auto-deprecate threshold raised to 7.
Migration `sources.0005_source_last_enriched_at` added with backfill
`last_enriched_at = fetched_at`.

---

## Phase 3 — Pilot 1 + finding fixes (2026-05-14) ✅

First real-LLM pilot ran 3 RSS + 3 GitHub MCP dry/apply runs and
produced one DRAFT (`maven-tools-mcp-server`). Five findings:

1. `AppCapability.note` empty on new-app path (evidence stayed in
   `Source.payload` but not on the row).
2. `use_cases` silently dropped — `upsert_app_from_draft` only
   attaches existing slugs.
3. Cost = $0 (per-model env vars unset → Finding F2).
4. `proposed_verdict` came back empty.
5. Host→docker-broker route requires `CELERY_TASK_ALWAYS_EAGER` from
   host venv (→ Finding F4).

Fixes for #1, #2, #4 shipped in commit `2f8be42`:

* `AppDraft` gained `capability_evidence: dict[str,str]` and
  `use_cases: list[str]`.
* `attach_capabilities` now writes `note=evidence[key][:200]`.
  `attach_use_cases` resolves free-text labels via
  `get_or_create(slug=slugify(title), defaults={'title': title})`.
* `_enriched_to_app_draft` propagates evidence + use_cases.
* `persist_new_draft` falls back to `scope_summary` for empty verdicts.
* `enrich-new-v1.0` prompt: verdict mandatory, use_cases 3-7 verb-led.

Re-pilot produced Apps #9-11 (`forgemax`, `mcp-tools-py`, `gram`),
each with 6+ evidence-carrying capabilities, 6 use_cases, 279-309-char
verdicts. 129 tests green.

---

## Phase 3 — Docker rebuild + first approval pass (2026-05-15) ✅

Container image was pre-Phase-3; rebuilt via `docker compose build
web worker beat && docker compose up -d`. `/health/` green. Admin
Playwright sweep across `/admin/catalog/app/?status__exact=draft`,
`/admin/catalog/app/{9,10,11}/change/`,
`/admin/sources/source/?source_type__exact=github_mcp`,
`/admin/agent/agentrun/`, `/admin/agent/llmcalllog/`,
`/admin/agent/needsreviewqueueentry/` — all 200, evidence visible on
capability rows, sidebar publish-checklist populated.

Editor pass on Apps #9-11: set
`editorial_review_status=reviewed` + `platform_verification_status=
not_listed`, `transition_to_published` → quality_score=60/100 each.

Demo seed apps removed (6 apps × cascades = 30 rows deleted) and
`seed_demo.DEMO_APPS = []` so they don't return on next container
boot. Commits `adb3a90`, `8f6cab8`.

Side bug noted (preexisting): `/apps/<category-slug>/` shadowed by
`apps/<slug:slug>/` detail route (→ Finding F3).

---

## Phase 3 — Scale-up: gate OPEN (2026-05-15) ✅

Single command inside the rebuilt container:

```
docker compose exec -T -e AGENT_SOURCES_ENABLED=github_mcp web \
  python manage.py agent_run --source=github_mcp --limit=30 --apply
```

Result: `seen=30 relevant=22 skipped_existing=0 persisted=22`. 73%
relevance from raw GitHub topic-search traffic; every relevant
candidate produced a sanitized `EnrichedDraft` and a real
`AppCapability` / `Source.payload` row.

**Per-DRAFT shape across the batch:**

* Proposed verdict 243-408 chars (mean ~310). No empty verdicts.
* 5-8 evidence-carrying yes/no capability rows per app.
* 5-7 verb-led use-case slugs per app.
* Sampled URL liveness (6 URLs across 22 apps) all 200 except
  `mcp.apify.com` which 401's because the endpoint is authenticated.

**Bulk publish:** 21 of 22 cleared `get_publish_checklist` on first
read once the editor flipped the two editorial fields. The 22nd
(`#12 Trigger.dev`) failed on "at least one platform" because the
listing→platform inference in `_enriched_to_app_draft` only maps
`mcp-server` → `mcp`. Trigger.dev is a TypeScript workflow runtime,
not one of the five listing-type shapes; the model correctly refused
to tag it as `mcp-server` so the platform stayed empty. Left as DRAFT
for editorial judgment — captured as Finding F5.

**Gate after the bulk publish:**

```
Phase 3 -> Phase 4 gate: OPEN
Generated RSS/GitHub apps: 25 (draft=1, published=24, hidden=0)
Approval rate: 96.0%
LLM cost: $0.000000 total; $0.000000 per published app
LLM calls: 25 (real=25, mock=0)
Cost basis complete: yes
```

Public Playwright sweep confirmed: `/` shows "Trending now" + "Fresh
in the grid" with 18 real cards; `/apps/` renders 29 detail links;
`/apps/chrome-devtools-mcp/`, `/apps/mcp-memory-service/`,
`/apps/apify-mcp-server/` 200 with full long descriptions,
capability evidence quotes, similar-tools cross-links;
`/apps/?q=mcp` matches expected agent-generated cards.

Commit `fcd631f`.

---

## Prod deploy hardening (2026-05-15) ✅

Two prod-blockers landed in commit `5233f2f`:

* `docker/entrypoint.sh` no longer falls back to a hardcoded
  `admin / admin123` superuser. Bootstrap is opt-in: runs only when all
  three of `DJANGO_SUPERUSER_USERNAME / _EMAIL / _PASSWORD` are set in
  the environment; otherwise logs a skip notice and defers to
  `manage.py createsuperuser`.
* `config/settings/prod.py` reads `CSRF_TRUSTED_ORIGINS` from env via
  `Csv()`. Required once the catalog sits behind
  `llmappmarket.com + www` for any cross-origin form/XHR POST.
* `.env.example` documents both keys.

---

## Next plan (priority order)

### A. Quick ROI / close open findings

1. ~~**F5 — Listing→platform inference.**~~ **Closed in `ec249d1`.**
2. ~~**F2 — OpenAI per-model cost env vars.**~~ **Closed.** Per-role
   pricing keys (`AGENT_OPENAI_PRIMARY_*` / `AGENT_OPENAI_CHEAP_*`,
   each with input / cached / output channels) wired through
   `build_provider`. `_estimate_cost_usd` subtracts cached tokens from
   billable input and applies the cached-input price. New
   `manage.py agent_backfill_costs` re-priced 61 historical rows.
3. **F1 — Anthropic provider. DEFERRED.** Operator policy: prod
   release runs on OpenAI; Anthropic provider is post-prod. Note
   retained so it isn't lost if the policy changes later.
   `apps/agent/llm/client.py:184` still raises `NotImplementedError`.

### B. Phase 4 — re-actualization + official directories

1. **Re-actualization pipeline (`apps/agent/pipeline/reactualize.py`).**
   Input: `AppSnapshot` + fresh `FetchResult` list. Re-run enrichment,
   diff against `AppSnapshot`. Every App-field change goes to
   `NeedsReviewQueueEntry(kind=reactualized, payload=diff)`. Only
   `Source.last_enriched_at`, `Source.payload`, and `LinkHealth` auto-
   update. Vanish detection via existing `LinkHealth.consecutive_failures`
   pattern: 3 × 404 → `Source.is_active=False` +
   `NeedsReviewQueueEntry(kind=vanished)`. Beat task
   `reactualize_apps_batch(limit=20)` daily at 07:00 UTC. Setting
   `AGENT_REACTUALIZATION_INTERVAL_DAYS=30`.
2. **Link-checker beat.** `check_app_links_batch` already exists with
   the corrected selector / threshold; wire it into
   `CELERY_BEAT_SCHEDULE`.
3. **Official directories — ToS-gated.** ChatGPT App Directory /
   Claude Connectors / Gemini Apps. **Legal/ToS review required
   first.** If permitted, build conservative scrapers (1 RPS/domain,
   robots.txt, identifying UA); failure mode = Sentry + retry next
   run. Beat 3×/week.

### C. Phase 5 — observability / guardrails

1. **Budget hard-stop.** Beat task `agent_budget_check` (hourly): sum
   `LLMCallLog.cost_usd` for the current month. At 80% of
   `AGENT_MONTHLY_BUDGET_USD` → email alert + auto-disable discovery
   sources (re-actualization keeps running, it's the more valuable
   one). At 100% → pre-task hook refuses new agent work.
2. **Admin cost dashboard.** Aggregate `AgentRun` / `LLMCallLog` by
   day × source × model. Use the existing admin or a small
   `apps/agent/views.py` page.
3. **Eval pack.** 10-20 hand-labelled fixtures in `tests/agent/eval/`
   mapping raw source payload → expected `EnrichedDraft`. Run as
   `pytest tests/agent/eval/ --eval`. Regression gate at >5pp
   accuracy drop when prompts change.

### D. Catalog growth (operational, not code)

* Keep running `agent_run --source=github_mcp --limit=30..50 --apply`
  on demand to grow the published catalog. Each run currently produces
  ~15-22 publishable cards.
* Once Anthropic provider is in (Item A2), re-baseline the cost model
  with Claude Sonnet on primary and decide on a steady-state weekly
  discovery cadence.
* When the catalog has ≥ 50 apps per platform, re-enable cross
  pages (`/<platform>/<category>/`).
