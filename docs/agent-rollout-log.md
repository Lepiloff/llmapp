# Agent Pipeline — Rollout Log

Condensed status + history of the semi-autonomous LLM catalog pipeline
specified in `docs/agent-pipeline.md`. Old phases are summarised in one
paragraph with commit hashes; recent Phase 3 scale-up keeps full detail.

For forensic depth on any compressed entry, run `git log --follow` on
the file it points to. The `Append-only` convention from earlier
revisions is dropped in favour of keeping this doc usable as a session
bootstrap.

---

## Status snapshot — 2026-05-17 (Sprint 2)

* **Phase 0** — MCP Registry ingest stabilized. ✅
* **Phase 1 + 1b** — LLM enrichment for existing DRAFTs, real OpenAI
  provider live. ✅
* **Phase 2** — Admin review queue + bulk publish. ✅
* **Phase 3** — Discovery (RSS + GitHub MCP), new-app enrichment,
  README fetch, manual CLI, gate report. ✅ **Phase 3 → Phase 4 gate
  is OPEN.**
* **Phase 4** — Re-actualization pipeline + vanish detection ✅.
  Beat-gated by `AGENT_REACTUALIZATION_ENABLED` (default False), runs
  daily at 07:00 UTC after the 05:00 link-checker batch. Official
  directories (ChatGPT App Directory / Claude Connectors / Gemini
  Apps) still ToS-blocked.
* **Phase 5** — Budget hard-stop ✅, admin cost dashboard ✅, eval
  pack scaffolding + 3 baseline fixtures ✅.
* **Sprint 1 — Prod-readiness hardening (2026-05-17)** ✅ — closed 8
  P1 findings from the architectural review: SECRET_KEY hardening,
  /health/ Celery worker check, unscheduled beat tasks, sitemap cache
  + invalidation, AppPlatform editor-edit preservation, FTS taxonomy
  coverage, per-domain rate limiter (cross-process via Redis),
  retention policy.
* **Sprint 2 — P2 follow-on (2026-05-17)** ✅ — closed 7 P2 findings:
  /go/ rate-limit, WhiteNoise prod static, capability evidence
  200→500, MCP Registry 404 → Sentry, HEAD→GET link-check fallback,
  UseCase merge admin tool, SLA dashboard for review queue. 303
  tests passing (+28). Only F1 (Anthropic provider, policy-deferred)
  and B3 (official directories, ToS-gated) remain in the
  open-findings list.

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
  # Phase 4 re-actualization: off by default; daily beat at 07:00 UTC
  # picks AGENT_REACTUALIZATION_BATCH_SIZE apps whose freshest source
  # is older than AGENT_REACTUALIZATION_INTERVAL_DAYS.
  AGENT_REACTUALIZATION_ENABLED=False
  AGENT_REACTUALIZATION_INTERVAL_DAYS=30
  AGENT_REACTUALIZATION_BATCH_SIZE=20
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
| F4 | ~~Host → docker-broker route~~ | ~~`apps/agent/management/commands/agent_run.py`~~ | **Closed in `0e002ac`** — `ensure_eager_if_broker_unreachable` auto-flips host-venv runs to eager mode with a stderr warning |
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

## Phase 5 round-2 — admin dashboard + eval pack (2026-05-16) ✅

Two follow-ups landed after the prod-rebuild dry-run pilot:

* **C2 — Admin cost dashboard** (`255ac2c`). `/admin/agent/agentrun/
  cost-dashboard/`, reachable from the AgentRun changelist via a
  "Cost dashboard" object-tool button. Shows current-month spend +
  budget bar, the two latches as colored pills, per-day cost trend
  (last 30 days), per-model + per-source breakdown for the current
  month, and top 10 expensive AgentRuns linked to detail.
* **C3 — Eval pack scaffolding** (`69ccf25`). `tests/agent/eval/`
  with `--eval` flag, fixture loader, and 3 baseline validation
  fixtures captured from the 2026-05-15 Phase 3 batch (forgemax,
  mcp-tools-py, triggerdev). Replays saved LLM raw outputs through
  `validate_enriched_draft` against a TaxonomySnapshot built from
  `seed.json` (pure, no DB) and asserts byte-for-byte equality on
  the sanitized result. Future `--eval-llm` extension will layer
  real-LLM regression on top — kept as a separate flag because each
  invocation costs real money (~$0.006/fixture).
* **F4 — Host→docker-broker** (`0e002ac`). The "use container path"
  runbook workaround became an auto-fallback: a 1-second TCP probe
  to `CELERY_BROKER_URL` runs at the top of `manage.py agent_run`;
  on DNS or connect failure the helper flips Django's
  `CELERY_TASK_ALWAYS_EAGER=True` in process and writes one stderr
  warning. Inside the container the probe succeeds and the helper
  is a no-op.

Use-case noise discovered during the 2026-05-16 dry-run probe was
addressed in commit `3d2c7ce`: `ReactualizationDiff.is_empty()` no
longer counts use-case slug churn as drift, since LLM phrasing
variance produces a stable +N -N churn every cycle. The delta still
rides into the queue-entry payload when *anything else* drifted.

Total: **210 passing** in default sweep + 3 eval fixtures behind
`--eval`. The only open items left on the rollout-log are F1
(Anthropic, policy-deferred) and B3 (official directories,
ToS-blocked).

---

## Phase 5 — Budget hard-stop (2026-05-15) ✅

`BudgetMonthState` (one row per UTC month) is the persistent source of
truth for whether new agent work is allowed. Beat task
`agent_budget_check` runs hourly at :15, sums every non-mock
`LLMCallLog.cost_usd` written this month, and upserts the row.

Two latching thresholds, each fires exactly once per month:

* **80% of `AGENT_MONTHLY_BUDGET_USD`** → set
  `discovery_disabled_at`. The discovery batch (`_run_discovery_batch`)
  returns `skipped: budget_threshold` before iterating candidates.
  Re-actualization and on-demand enrichment keep running — operators
  prefer "spend the rest of the budget on keeping the catalog fresh,
  not on net-new cards."
* **100%** → set `hard_stop_at`. `assert_agent_can_run` raises
  `AgentBudgetExceeded`. The guard runs at the top of
  `run_enrich_existing_draft` / `run_enrich_new_app` /
  `run_reactualize_app` BEFORE the `AgentRun` row is created, so the
  audit trail of "what got blocked" lives only in the beat task that
  flipped the flag.

Email recipients come from `AGENT_BUDGET_ALERT_EMAILS` →
`AGENT_REVIEW_DIGEST_EMAILS` → `SUBMISSIONS_NOTIFY_EMAILS`. Missing
recipients are logged but the latch still flips — workers must gate
correctly even when alerting is misconfigured.

Manual reset path (admin → BudgetMonthState):
1. Bump `AGENT_MONTHLY_BUDGET_USD` (the next beat tick clears both
   latches automatically when utilization drops below threshold).
2. Or clear `discovery_disabled_at` / `hard_stop_at` directly. The
   admin lists each month with utilization %, both latches, and the
   cost/budget pair.

Tests: 15 in `tests/agent/test_budget_hard_stop.py`. Total
`tests/`: **193 passing**.

Commits land in 5A–5D, see git history. F1 (Anthropic provider) is
still deferred per operator policy; nothing else is open.

---

## Phase 4 — Re-actualization + vanish detection (2026-05-15) ✅

`apps/agent/pipeline/reactualize.py::compute_reactualization` is the
pure-Python diff that compares an `AppSnapshot` against a fresh
`EnrichedDraft` and returns a `ReactualizationDiff` covering text
fields, capabilities (with new evidence + confidence), taxonomy
additions and disappearances, and editorial proposals. **The diff is
the only output** — published cards are editor-owned, so the bridge
never auto-applies anything.

`apps/agent/persist.py::queue_reactualization` is the Django bridge:
non-empty diffs become one `NeedsReviewQueueEntry(kind=REACTUALIZED)`;
empty diffs still advance `Source.last_enriched_at` so the cadence
window resets without spamming the editor. `Source.payload` carries
an `agent_reactualization` audit block.

`apps/agent/tasks.py::run_reactualize_app` orchestrates one app:
picks the freshest re-actualizable `Source`, re-fetches via the
per-source-type fetcher (GitHub MCP rows go through the README API,
everything else through `fetch_url_text`), runs `enrich_new_app`,
diffs against the snapshot, persists. `reactualize_apps_batch` is the
beat wrapper, gated by `AGENT_REACTUALIZATION_ENABLED` (default
False); `dry_run=True` bypasses the flag for manual probes.

Beat schedule entry runs daily at **07:00 UTC**, after the 05:00
link-checker batch — so vanished sources flip `Source.is_active=False`
*before* re-actualization runs and we never re-fetch a URL that the
link checker already buried.

Selector `pending_reactualization_app_ids` orders NULLS FIRST so
apps never enriched at all drain ahead of the long-tail refresh
cycle. `_REACTUALIZABLE_SOURCE_TYPES` is an opt-in allowlist
(MCP_REGISTRY, AGENT_ENRICH, RSS_DISCOVERY, GITHUB_MCP); MANUAL
cards are off-limits.

**Vanish detection on link checker** (`apps/sources/tasks.py`): at
the exact failure increment that crosses
`AUTO_DEPRECATE_FAILURE_THRESHOLD=7` on `official` or `install`
targets, the link checker now also flips `Source.is_active=False` for
any source whose `source_url` matches the dead URL and writes one
`NeedsReviewQueueEntry(kind=VANISHED, payload={target, url,
status_code, consecutive_failures, sources_deactivated})`. Fires
exactly once per crossing — subsequent failures don't spam the queue,
recovery resets the counter so a later breakage queues a fresh event.

Tests: 24 new across `tests/agent/test_reactualize.py` (10),
`tests/agent/test_reactualize_task.py` (8), and additions to
`tests/sources/test_link_checker.py` (6) — `tests/` is 178 passing.

Commits: `31b3e8d` (pure-Python diff), `95abb60` (Django bridge),
`4395493` (orchestrator + beat + settings), `538c12b` (vanish
detection in link checker).

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

## Sprint 1 — Prod-readiness hardening (2026-05-17) ✅

Architectural review (`docs/project-overview-ru.md` + this log)
surfaced 8 P1 findings — risks that didn't block earlier phases but
needed closing before the prod release. All landed in a single
sprint with regression tests; total suite went from ~210 to 263
passing (+53 new, no regressions).

The eight findings, by review-numbering:

**P1.1 — Unscheduled beat tasks.** `calculate_trending_scores`,
`ping_search_engines`, `cleanup_old_analytics_data`,
`update_popular_searches`, `generate_seo_reports` were defined but
never wired into `CELERY_BEAT_SCHEDULE`. Consequences: trending
homepage block ran on live `qs.trending()` only (cost grows with
traffic); Google/Bing never pinged after sitemap rebuild;
ClickEvent/PageView/SearchLog/LinkCheckResult/LLMCallLog grew
without retention. Fix: 9 new beat entries (5 backfilled + 4 new
retention tasks). Verified by `tests/core/test_beat_schedule.py`
which asserts every scheduled task is importable.

**P1.2 — `search_vector` didn't index taxonomy.**
`refresh_app_search_vector` built the FTS vector from `name` /
`short_description` / `developer_name` / `long_description` only;
platforms / categories / use_cases were aggregated into
`search_index_text` but that field wasn't referenced by the
`SearchVector` expression that `apps/search/views.py::app_search`
queries. Queries like "mcp servers" or "developer tools" missed
cards titled differently. Fix: `search_index_text` now aggregates
platform/category/use-case names and the vector includes it via
`Coalesce("search_index_text", Value(""), output_field=TextField())`
with weight C — name-match (A) still outranks taxonomy match.
Regressions: `tests/search/test_fts_taxonomy_match.py`.

**P1.3 — `rebuild_sitemap` didn't rebuild.**
`apps/seo/tasks.py::rebuild_sitemap` ran `Client().get('/sitemap.xml')`
every 30 min — Django sitemap view regenerates on every request, no
cache layer existed, so the task wasted DB roundtrips without
benefit. Fix: sitemap view wrapped in
`cache_page(60*30, key_prefix='sitemap_v1')`; `rebuild_sitemap`
purges via `cache.delete_pattern("*sitemap_v1*")` with a `cache.clear()`
fallback for backends without pattern support;
`apps/seo/signals.py::post_save(App)` triggers
`transaction.on_commit(invalidate_sitemap_cache)` so freshly
published cards appear within seconds, not the 30-min TTL.
Regressions: `tests/seo/test_sitemap_cache.py` (4).

**P1.4 — `AppPlatform` metadata clobbered on re-discovery.**
`apps/sources/upsert.py::attach_platforms` used
`update_or_create(defaults={region_availability, supported_plans,
scope_summary, metadata, ...})` — every re-discovery of the same
`external_id` silently overwrote editor-set fields, breaking Hard
constraint #2 ("LLM never overwrites editor edits") on DRAFT cards.
Fix: split into create-path (first time fills everything from draft)
and update-path (only fills empty/UNKNOWN fields; `metadata` is
shallow-merged with editor edits winning on conflicts;
`last_verified_on_platform_at` always updates). Constraint extended
to AppPlatform in `docs/agent-pipeline.md` #2. Regressions:
`tests/sources/test_attach_platforms.py` (7).

**P1.5 — `SECRET_KEY` insecure default in prod.**
`config/settings/base.py:23` defaulted to
`"insecure-dev-key-change-me"`; `prod.py` didn't override. A missing
`.env` SECRET_KEY would boot prod with a known-key — session
forgery + signed-data tampering. Fix: `config/settings/prod.py` now
calls `config("SECRET_KEY")` without default and raises
`ImproperlyConfigured` on empty/insecure values. Regressions:
`tests/core/test_prod_settings.py` (3).

**P1.6 — `AGENT_RATE_LIMIT_RPS_PER_DOMAIN` was a dead setting.**
Defined in `base.py:385`, read nowhere. RSS, GitHub Search,
`fetch_url_text` hit upstream without per-domain throttling — at
`discover_github_mcp --limit=30` that's 30 README requests in a
burst, only saved by GitHub's authoritative rate-limit headers.

Initial fix: `apps/agent/pipeline/rate_limit.py::DomainRateLimiter`
(thread-safe token-bucket keyed by URL hostname); wired into
`fetch_url_text`, `GitHubMCPSearchSource._requests_get_json`,
`_requests_get_json_with_token`, `RSSFeedSource._requests_fetch`.

**Follow-up fix (cross-process correctness):** the initial limiter
was process-local, but `worker --concurrency=2` runs two child
processes each carrying its own bucket — actual rate doubled,
violating the documented guarantee. Replaced with a Redis-backed
implementation:
* `apps.agent.rate_limit_redis.RedisDomainRateLimiter` — a tiny Lua
  script reads the persisted "next allowed timestamp" for a host,
  computes the wait time, advances the timestamp atomically, returns
  the wait so the caller sleeps locally. All worker processes
  coordinate on the same Redis key (`agent:rate_limit:<host>`).
* Pipeline layer kept pure-Python: `apps/agent/pipeline/rate_limit.py`
  now exposes `NoopRateLimiter` (default), `InMemoryDomainRateLimiter`,
  and `get/set_default_limiter()` injection point. No Django imports.
* `AgentConfig.ready()` calls `build_limiter_from_settings()` which
  pings Redis and registers either the Redis-backed limiter or
  (soft-fallback with logged warning) the in-memory one if Redis is
  unreachable at startup.

Regressions: `tests/agent/test_rate_limit.py` (10),
`tests/agent/test_rate_limit_redis.py` (10).

**P1.7 — Retention policy absent for unbounded tables.**
`LLMCallLog`, `EnrichmentTask`, `AgentRun`, `ClickEvent`, `PageView`,
`SearchLog`, `LinkCheckResult` grew without eviction. At current
scale (24 apps) this didn't bite yet; at 1k+ apps `LinkCheckResult`
alone generates ~4k rows/day. Fix: 4 cleanup tasks +
3 retention envs:
* `cleanup_old_agent_logs(180d)` — deletes `AgentRun` rows;
  `EnrichmentTask` and `LLMCallLog` cascade via FK. Pending
  `NeedsReviewQueueEntry` rows are preserved regardless of age.
* `cleanup_old_link_check_results(30d)` — `LinkCheckResult` only;
  `LinkHealth` is rolling-summary, never deleted.
* `cleanup_old_search_logs(90d)` — `SearchLog`; `PopularSearch` is
  rebuilt from the trailing 30-day window so 90 days is safe.
* `cleanup_old_analytics_data(90d)` — was already implemented in
  `apps/analytics/tasks.py` but never scheduled; wired into beat.
All four run sunday 04:00-04:45 UTC. Envs:
`AGENT_LOG_RETENTION_DAYS=180`,
`SOURCES_LINK_CHECK_RETENTION_DAYS=30`,
`SEARCH_LOG_RETENTION_DAYS=90`. Regressions:
`tests/agent/test_cleanup_old_agent_logs.py` (2).

**P1.8 — `/health/` blind to dead Celery worker.**
Probed DB + Redis + pg_trgm. A live `web` container in front of a
crashed worker still returned 200 — discovery / re-actualization /
budget-check silently stopped. Fix:
`apps/core/healthcheck.py::_check_celery_worker` calls
`celery_app.control.ping(timeout=1.5)` and returns False on empty
reply or broker error. Latency stays bounded so /health/ remains
snappy under a load balancer. Regressions: 4 added to
`tests/core/test_healthcheck.py` covering ok-ping, no-reply,
broker-down, and the integration 503-on-celery-down path.

**Sprint 1 follow-up (post-review fixes, same day):**
A second-pass review surfaced five tightening items that landed on
top of the initial Sprint 1 commits:
1. **Cross-process rate limiting** — addressed in P1.6 above.
2. **Pure-Python pipeline boundary restored.**
   `apps.agent.pipeline.rate_limit` no longer imports
   `django.conf.settings`; the Django bridge registers a configured
   limiter at app startup. Pipeline import-time is Django-free again
   (`grep -nE "from django|import django" apps/agent/pipeline/` shows
   only one pre-existing local import inside `reactualize.py`).
3. **`attach_platforms` race tightened.**
   Snapshot-then-update now runs inside `transaction.atomic()` with
   `select_for_update()` on the existing `AppPlatform` row, mirroring
   `apps.agent.persist._apply_field_updates`. An editor write
   between the agent's read and write either blocks the agent (and
   the agent sees the fresh editor value under the lock, preserving
   it) or blocks the editor (and the editor's transaction re-reads
   our write before deciding what to overwrite). Regression:
   `tests/sources/test_attach_platforms.py::test_concurrent_editor_edit_survives_attach_platforms`
   (threading-based, ~0.3s).
4. **Sitemap signal covers unpublish + delete.**
   The first version only invalidated when the saved instance's new
   status was `PUBLISHED`. Now `post_save` fires unconditionally and
   a new `post_delete` receiver handles hard-deletion. Stale URLs
   can no longer linger in the cached XML for up to 30 minutes after
   a card is hidden or removed. Regressions added for both paths.
5. **`ping_search_engines` timeout.**
   Both `urllib.request.urlopen` calls now use an explicit
   `timeout=10`; a slow Google/Bing endpoint would previously pin a
   Celery worker indefinitely. Unused `urlencode` import removed.

Total after follow-up: 275 passing (+12 vs initial Sprint 1).

**Doc updates accompanying Sprint 1:**
* `docs/agent-pipeline.md`: Hard constraint #2 extended to
  `AppPlatform`; #8 split into 8a (rate-limit hard-enforced
  cross-process via Redis) and 8b (robots.txt — TBD for B3). Phase
  0 pre-check now records the FTS taxonomy fix. New "Retention"
  subsection under Phase 5. Critical-files section lists the new
  Sprint 1 modules. ASCII tree of `apps/agent/` updated to show
  `rate_limit_redis.py` in the Django layer (outside `pipeline/`).

**What's still open:**
* **F1 — Anthropic provider.** Stub raises `NotImplementedError`.
  Deferred per operator policy until after prod release; planned as
  the first post-release item (Claude Sonnet 4.6/4.7 on primary
  role, OpenAI gpt-5.4-mini moves to cheap role or stays as
  comparison).
* **B3 — Official directories.** Still legal/ToS-blocked. When
  cleared, `apps/agent/sources/chatgpt_directory.py` /
  `claude_connectors.py` / `gemini_apps.py` will follow the
  conservative design constraints already enforced upstream
  (`DomainRateLimiter` covers the per-domain RPS guarantee; robots
  parser is the only piece still to write — Hard constraint #8b).

---

## Sprint 2 — P2 follow-on (2026-05-17) ✅

Sprint 1 closed the prod-blocker P1 list. Sprint 2 picks up the
P2 backlog from the same architectural review — quality-of-life
fixes that aren't release blockers but tighten the operational
surface. 303 passing tests at end (+28 vs Sprint 1).

**P2.9 — /go/<slug>/ rate-limit.**
`apps/analytics/views.py::outbound_redirect` now decorated with
`@ratelimit(key='ip', rate='60/m', block=True)`. Each request
writes one `ClickEvent`, which feeds the trending score; without a
cap an attacker could inflate trending rankings or simply flood the
DB. 60/min is high enough that real users never hit it, low enough
that scripted abuse trips a 403. Regressions:
`tests/analytics/test_outbound_rate_limit.py`.

**P2.14 — WhiteNoise for prod static.**
The docker-compose `nginx` profile is optional (set via
`--profile production`); without it, gunicorn alone wouldn't serve
`/static/*` and admin CSS/JS 404'd. WhiteNoise installed as a hard
dependency (`pyproject.toml`), middleware injected immediately
after `SecurityMiddleware` in `config/settings/prod.py`, storage
backend set to `CompressedManifestStaticFilesStorage` (cache-busting
hashes + gzip/brotli). Regressions:
`tests/core/test_prod_settings.py` (+2: middleware order, storage
backend).

**P2.12 — AppCapability.note 200→500 chars.**
LLM citations from README sentences were routinely cut mid-quote
at 200. Field widened to 500 via migration
`apps/catalog/migrations/0002_expand_capability_note_to_500.py`.
Truncation callsites in `apps/agent/persist.py` and
`apps/sources/upsert.py` bumped from `[:200]` to `[:500]`.
Regressions: `tests/catalog/test_capability_evidence_length.py`.

**P2.13 — MCP Registry 404 surfaces in Sentry.**
The registry endpoint has been returning 404 since ~2026-05-15; the
ingest task swallowed the exception with `logger.exception(...)`
and reported counters=0 — invisible in alerting. Added
`_report_to_sentry(exc, base_url, cursor)` in
`apps/sources/mcp_registry.py` that captures via `sentry_sdk` with
a stable `fingerprint=["mcp-registry-unreachable", str(status)]`
so all 404s coalesce into one Sentry issue across daily retries.
Soft dependency — no-ops when `sentry_sdk` isn't importable
(local dev, CI). Regressions:
`tests/sources/test_mcp_registry_sentry.py` (+3).

**P2.10 — HEAD-only link check → GET fallback.**
`apps/sources/tasks.py::_check_app_links` issued HEAD-only requests.
Some SaaS endpoints (and many GitHub Pages sites) return 405 / 501
on HEAD even when live; those URLs were tipping
`consecutive_failures` every cycle and tripping auto-deprecate
after 7 cycles. New `_probe_url(url)` helper: HEAD first, fall back
to a tiny ranged GET (`Range: bytes=0-1023`, `stream=True`) when
HEAD returns 403 / 405 / 501. Regressions:
`tests/sources/test_probe_url.py` (+11).

**P2.11 — UseCase merge admin tool.**
LLM enrichment produces a long tail of synonym use-cases
(`"Generate sales reports"` vs `"Sales report generation"` vs
`"Generate a sales report"` — three slugs for one editorial
concept). New service function
`apps/catalog/services.merge_use_cases(target_id, source_ids)`
collapses sources into a target row inside `transaction.atomic()`
+ `select_for_update()`, with safe deduplication when an app
already has the target row (unique_together protection). Wired as
admin action `Merge selected use-cases into one canonical row` on
`UseCaseAdmin` with a two-step intermediate page so editors pick
the canonical slug before applying. List shows `app_count`
annotation so editors can prioritize collapsing the heavy-hitter
synonyms first. Template:
`templates/admin/catalog/usecase/merge_confirm.html`.
Regressions: `tests/catalog/test_merge_use_cases.py` (+5).

**P2.15 — SLA dashboard for the review queue.**
`/admin/agent/needsreviewqueueentry/sla-dashboard/` — one-page
snapshot for the editor backlog. Reports: pending count, overdue
count (>`AGENT_REVIEW_QUEUE_SLA_DAYS=14` days unresolved), oldest
age in days, breakdown by kind, top-10 oldest pending entries with
deep links. Status block flips between OK and OVERDUE. Editors
and oncall use this instead of filtering the changelist by hand.
Custom URL registered via `NeedsReviewQueueEntryAdmin.get_urls`;
reachable URL also stashed in `extra_context['sla_dashboard_url']`
for any future changelist-template integration. Threshold is read
from Django settings at request time so operators tune the SLA
without a code release. Regressions: `tests/agent/test_sla_dashboard.py`
(+4).

**Sprint 2 follow-up (post-review fixes, same day):**
Three tightening items landed on top of the initial Sprint 2 commits
after a second-pass review:
1. **`/go/` rate-limit now XFF-aware.**
   `key='ip'` reads `REMOTE_ADDR`, which behind nginx / ALB is the
   proxy's address — every real client would share one bucket and a
   single noisy user could 403 outbound clicks for everyone.
   Replaced with a callable `_xff_aware_ip_key` that mirrors
   `apps.analytics.utils.get_client_ip`, picking the first hop from
   `X-Forwarded-For`. Same client-IP policy now applies across click
   tracking and throttling. Regression:
   `test_throttle_bucket_keyed_by_xff_not_remote_addr`.
2. **UseCase merge refreshes search vectors.**
   `merge_use_cases` mutates `AppUseCase` rows through the ORM
   directly, bypassing the `m2m_changed` signal that
   `apps/catalog/signals.py` uses to schedule search-vector refresh
   on use-case writes. Sprint 1 made use_case titles part of
   `search_vector`, so merged apps were keeping stale matches for
   the deleted synonym title until the nightly rebuild. Fix:
   collect affected `app_ids` during the merge, then
   `transaction.on_commit` enqueue `refresh_search_vector_task.delay`
   for each. Regression: `test_merge_enqueues_search_vector_refresh_for_affected_apps`.
3. **SLA window actually configurable.**
   `SLA_PENDING_DAYS = 14` was hard-coded despite the comment
   claiming env-configurability. Added
   `AGENT_REVIEW_QUEUE_SLA_DAYS` setting (default 14), a
   `_sla_pending_days()` helper that reads it at request time, and
   documented the env in `.env.example`. Regression:
   `test_sla_window_honors_settings_override`.

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

1. ~~**Re-actualization pipeline.**~~ **Closed.** `reactualize.py` +
   `queue_reactualization` + `run_reactualize_app` +
   `reactualize_apps_batch` beat. Link-checker now also queues a
   `kind=vanished` review entry on threshold crossing.
2. ~~**Link-checker beat.**~~ **Closed (already wired).**
   `check_app_links_batch` was scheduled in
   `CELERY_BEAT_SCHEDULE` from the Phase 4-prereq slice — the rollout
   log's earlier "still to be built" line was stale.
3. **Official directories — BLOCKED on legal/ToS review.** ChatGPT
   App Directory / Claude Connectors / Gemini Apps. Cannot implement
   conservative scrapers without explicit ToS clearance per operator
   policy. When unblocked, design constraints: 1 RPS/domain
   (already enforced by `DomainRateLimiter` — Sprint 1, just plug
   each new source's fetcher through `get_default_limiter()`),
   robots.txt parser (still to write — Hard constraint #8b),
   identifying User-Agent, failure mode = Sentry + retry
   next run, beat 3×/week. **Action: route to legal/business owner
   for ToS clearance before any code is written.**

### C. Phase 5 — observability / guardrails

1. ~~**Budget hard-stop.**~~ **Closed.** `BudgetMonthState` +
   `agent_budget_check` hourly beat + `assert_agent_can_run` gates
   in every orchestrator.
2. ~~**Admin cost dashboard.**~~ **Closed.** `/admin/agent/agentrun/
   cost-dashboard/` aggregates per-day / per-model / per-source +
   top-10 expensive runs + latch pills (`255ac2c`).
3. ~~**Eval pack.**~~ **Closed (scaffolding + 3 baseline fixtures).**
   `tests/agent/eval/` with `--eval` flag; replay validation regression
   on saved LLM raw outputs against `seed.json` taxonomy (`69ccf25`).
   Extending the fixture set is a one-shell-line ORM dump; the
   `--eval-llm` real-LLM mode is the next layer when prompt iteration
   needs it.

### D. Catalog growth (operational, not code)

* Keep running `agent_run --source=github_mcp --limit=30..50 --apply`
  on demand to grow the published catalog. Each run currently produces
  ~15-22 publishable cards.
* Once Anthropic provider is in (Item A2), re-baseline the cost model
  with Claude Sonnet on primary and decide on a steady-state weekly
  discovery cadence.
* When the catalog has ≥ 50 apps per platform, re-enable cross
  pages (`/<platform>/<category>/`).
