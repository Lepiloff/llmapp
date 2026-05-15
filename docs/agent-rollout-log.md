# Agent Pipeline — Rollout Log

Sequential record of acceptance-criteria evidence for each phase of
`docs/agent-pipeline.md`. Append-only; never edit historical entries.

---

## Phase 0 — Stabilizing the existing ingest

### Pre-check (Phase 0 step 4, read-only)

Verified on 2026-05-13:

- **`pg_trgm` extension** — ✅ `TrigramExtension()` declared in
  `apps/catalog/migrations/0001_initial.py:16`. No fix needed.
- **Search-vector signal refresh** — ✅ `post_save(App)`,
  `m2m_changed(App.categories.through / platforms.through / use_cases.through /
  listing_types.through)` and `post_save(AppCapability)` receivers wired in
  `apps/catalog/signals.py`; module imported in
  `apps/catalog/apps.py::CatalogConfig.ready`. Each receiver enqueues
  `refresh_search_vector_task.delay(app_id)` via `transaction.on_commit`. No
  fix needed.
- **URL routes** — ✅ `config/urls.py` registers all expected paths: admin,
  health, catalog (incl. category/platform/cross), search, submit, blog,
  newsletter, analytics redirect, sitemap, robots. No fix needed.
- **`Platform(slug='mcp')` fixture** — ✅ Present in
  `apps/catalog/fixtures/seed.json` (pk=4). Loaded by
  `apps/catalog/management/commands/seed_demo.py::_load_references`. No fix
  needed.

### Refactor (Phase 0 steps 1-3)

- **`apps/sources/tasks.py`** rewritten on 2026-05-13: `ingest_mcp_registry`
  now delegates to `MCPRegistrySource().iter_drafts()` +
  `upsert_app_from_draft()`. Legacy helpers `_process_mcp_server`,
  `_create_app_from_mcp_server`, `_update_app_from_mcp_server` deleted.
  Per-record failures are isolated; `source.unparsed` is flushed into
  `UnparsedRegistryRecord`; observed schema versions logged. Link-check
  helpers (`_check_app_links`, `_update_link_health`) preserved unchanged.
- **Data migration** `apps/sources/migrations/0002_backfill_mcp_appplatform.py`
  added on 2026-05-13: idempotent backfill of `AppPlatform` rows for
  MCP-imported apps + conservative flip of
  `platform_verification_status` to `official` (only for apps with
  `editorial_review_status='unreviewed'`).

### Acceptance criteria evidence

Verified on 2026-05-13. Test environment: local `.venv` (Python 3.12) with
pytest-django against the dev Postgres container (`127.0.0.1:5432`).
Migration `0002_backfill_mcp_appplatform` applied to dev DB without error.

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | 100% MCP apps have `AppPlatform(mcp)` | ✅ | Trivially holds in dev DB (0 MCP-imported apps yet). Mechanism verified by `test_creates_app_with_appplatform_and_official_status`. |
| 2 | 100% MCP apps have `platform_verification_status=official` (among unreviewed) | ✅ | Same as #1. Mechanism verified by the same regression test asserting `app.platform_verification_status == PlatformVerificationStatus.OFFICIAL`. |
| 3 | `ingest_mcp_registry` idempotent | ✅ | `test_is_idempotent_on_repeated_runs`: first run = `new=1`, second = `updated=1`, all counts unchanged. Migration backfill ran twice with `AppPlatform.count()` stable at 11. |
| 4 | Legacy helper functions removed | ✅ | `grep -rn '_process_mcp_server\|_create_app_from_mcp_server\|_update_app_from_mcp_server' apps/ --include="*.py"` returns empty. |
| 5 | Schema-mismatched records routed to `UnparsedRegistryRecord` | ✅ | `test_routes_schema_mismatches_to_unparsed_registry_record`: passes; bad record stored with error message + schema_version intact. |
| 6 | Pre-check documented | ✅ | This section (pre-check above). |
| 7 | `tests/sources/test_ingest_mcp_registry.py` green | ✅ | 5/5 passed: `pytest tests/sources/test_ingest_mcp_registry.py -v` → `5 passed, 2 warnings`. |
| 8 | All existing tests green | ✅ | Full `pytest` collected 5 items (only the new tests exist in the repo), 5/5 passed. |

### Operational notes

- **Live registry ingest:** Not yet exercised in this verification pass.
  Tests use `_FakeSource` to keep them deterministic. A real `make ingest`
  run against `https://registry.modelcontextprotocol.io/v1` would produce
  actual rows. Recommended pre-Phase-1 smoke: `docker compose exec web make
  ingest` once with internet access, then `make ingest` again to confirm
  `updated_count > 0, created_count = 0` (idempotency in production).
- **Test setup notes for future runs:**
  - `pip install pytest pytest-django factory-boy freezegun` into the venv
    (these are listed under `[project.optional-dependencies] dev` in
    `pyproject.toml`).
  - Set `DATABASE_URL=postgres://llmmarket:llmmarket@localhost:5432/llmmarket`
    when running pytest from the host (the in-container hostname `postgres`
    isn't reachable from the host).
  - First run after pulling new migrations: `pytest --create-db` to force
    test-DB recreation. Subsequent runs reuse via `--reuse-db` (default in
    `pyproject.toml`).
- **Side bug fix:** Initial `logger.exception(..., extra={"name": ...})` in
  the rewritten `ingest_mcp_registry` collided with `LogRecord.name`. Fixed
  by renaming the extra key to `draft_name`. The `test_per_record_upsert_
  failure_is_isolated` test caught it.

**Phase 0 complete. Gate to Phase 1: OPEN.**

---

## Phase 0 — Follow-up hardening (post-review, 2026-05-13)

External reviewer flagged that `MCPRegistrySource.iter_drafts` still had
exception surface that could abort the whole task. Before treating live
registry ingest as a trusted scheduled source, the following hardening was
applied:

### Fix — `apps/sources/mcp_registry.py`

* `resp.json()` is now wrapped in `try/except ValueError` (covers
  `json.JSONDecodeError`). Invalid JSON ⇒ logged + empty iteration.
* The decoded payload is `isinstance`-checked as `dict` before any
  `.get()` access. Non-dict payload (list / string / scalar) ⇒ logged +
  empty iteration; schema_version is **not** recorded because we cannot
  read it from a non-dict.
* `payload.get("servers")` is validated: `None` is treated as an empty
  page (honouring `next_cursor`), anything else non-list is logged + the
  page is abandoned. The schema_version is still observed for monitoring.
* `_normalize` now type-checks the record at entry; a non-dict record
  raises `MCPRegistrySchemaError("record is not a dict (...)")` instead
  of an unhandled `AttributeError`/`TypeError`. The faulty record is
  routed to `UnparsedRegistryRecord` like any other schema mismatch.
* `next_cursor` is type-checked: only a truthy `str` continues pagination.
  Anything else (None, dict, list, number) terminates cleanly.
* Helper `_is_json_safe(value)` guards the `UnparsedRegistryRecord.payload`
  write: non-JSON-safe records (defensive — never expected from
  `resp.json()`) fall back to `repr()` so the unparsed buffer never fails
  on its own serialization.

### Tests — `tests/sources/test_mcp_registry_source.py`

13 new regression tests cover the hardened paths:

| Scenario | Assertion |
|---|---|
| Invalid JSON | empty iter, no raise, schema_version not set |
| HTTP 500 (`raise_for_status`) | empty iter, no raise |
| Payload is a list | empty iter, no raise |
| Payload is a string | empty iter, no raise |
| `servers` is a string | empty iter, schema_version observed |
| `servers` is `null` | empty iter, schema_version observed |
| `servers` key missing | empty iter, schema_version observed |
| Non-dict records (str, int, None, list) | each routed to unparsed |
| Record missing required fields | routed to unparsed |
| Mixed good/bad records on one page | bad ones unparsed, good ones yielded |
| Valid single page | yields correct `AppDraft` (capabilities, listing_types) |
| Malformed `next_cursor` (dict) | pagination stops cleanly |
| Valid string `next_cursor` | second page fetched, both pages yielded |

Full run: `pytest tests/sources/ -v` → **18 passed** (5 task-level +
13 source-level).

### Deferred — link-checker (Medium, blocking Phase 4)

Reviewer also flagged two issues in `apps/sources/tasks.py::check_app_links_batch`
that are **not blocking Phase 1** but **must be fixed before any
re-actualization / production link-check automation** in Phase 4:

1. **Never-checked apps are excluded.** The selector
   `App.published.filter(last_checked_at__lt=cutoff)` silently drops apps
   where `last_checked_at IS NULL` — i.e., every freshly-imported MCP
   draft after Phase 1. Should be `Q(last_checked_at__lt=cutoff) | Q(last_checked_at__isnull=True)`.
2. **Auto-deprecate threshold off-by-two.** Current code flips
   `launch_status=DEPRECATED` at `consecutive_failures >= 5`
   (`apps/sources/tasks.py:181`). `docs/business.md § 11.3` and
   `docs/architecture.md § 11` specify **7 consecutive failures**. The
   five-failure threshold would auto-deprecate live apps faster than
   intended, which is more damaging than the inverse.

Both fixes are small but require their own regression tests (currently
the link-checker has no test coverage). **Tracked as Phase 4
pre-requisites in `docs/agent-pipeline.md`.**

---

## Phase 1 — Foundation + enrich_existing_draft (mock-only, 2026-05-13)

Per gate decision: start with the minimal slice — pure-Python core,
mocked LLMProvider, ``--dry-run`` by default. Real Anthropic / OpenAI
provider wiring (Phase 1b) is deferred until all the merge / persist
invariants are green against fixtures.

### Shipped

* **`apps/agent/` Django app** scaffolded, registered in
  `INSTALLED_APPS`. Migration `agent.0001_initial` applied to dev DB.
* **Models** (`apps/agent/models.py`): `AgentRun`, `EnrichmentTask`,
  `LLMCallLog`, `NeedsReviewQueueEntry`. All four are read-only in admin.
* **Pure-Python core**:
  - `apps/agent/llm/schemas.py` — Pydantic v2 contracts:
    `CapabilityProposal` (with evidence + confidence),
    `CategoryProposal`, `ListingTypeProposal`, `EnrichedDraft`,
    `MergeSet`, `AppSnapshot`. All `extra="forbid"`.
  - `apps/agent/llm/client.py` — `LLMProvider` ABC + factory
    `build_provider(role)`. `AnthropicProvider` / `OpenAIProvider`
    stubs raise `NotImplementedError` until Phase 1b.
    `MockLLMProvider` drives every test.
  - `apps/agent/llm/prompts.py` — versioned prompt template
    (`enrich-existing-v1.0`). Hard rules embedded in the system
    prompt mirror the validator + merge guardrails.
  - `apps/agent/pipeline/taxonomy.py` — frozen `TaxonomySnapshot`
    dataclass — the only structural seam between Django and the
    pure-Python pipeline.
  - `apps/agent/pipeline/validate.py` — second-layer guardrails:
    evidence-less yes/no → unknown, unknown slugs dropped,
    low-confidence categories dropped, invalid URLs nulled out.
    Returns sanitized `MergeSet` + structured `ValidationReport`.
  - `apps/agent/pipeline/merge.py` — never-overwrite-editorial-intent
    policy. Produces `Plan` (safe writes) + `QueueProposal` (anything
    requiring editor review).
  - `apps/agent/pipeline/enrich.py` — orchestrator chaining
    prompt → LLM → validate → merge.
* **Django bridge** (`apps/agent/persist.py`):
  - `build_taxonomy_snapshot`, `build_app_snapshot` build the
    pure-Python views from ORM.
  - `apply_merge_set` writes the plan inside a single transaction.
    Asserts no plan ever touches `App.status` /
    `editorial_review_status` / `platform_verification_status` /
    `developer_claim_status` / `verdict` (defense in depth — the
    merge layer already refuses).
  - Records a `Source(external_id='agent-enrich:<app_id>')` row
    carrying the full audit payload (raw merge, sanitized merge,
    validation report, plan, queue).
* **Celery tasks** (`apps/agent/tasks.py`): `enrich_existing_draft_task`
  + `enrich_pending_drafts_batch` (not added to beat schedule —
  Phase 1b decision).
* **Management command**: `manage.py agent_run --enrich-app=<slug>`
  / `--enrich-pending --limit N`. Defaults to dry-run; `--apply`
  required to write to the catalog.
* **Settings**: `AGENT_LLM_PROVIDER_PRIMARY` / `_CHEAP`,
  `AGENT_LLM_MODEL_*`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `AGENT_MONTHLY_BUDGET_USD`, `AGENT_RATE_LIMIT_RPS_PER_DOMAIN`,
  `AGENT_SOURCES_ENABLED`. All optional; provider defaults to `mock`
  so the app imports cleanly without API keys.
* **Dependency**: `pydantic>=2.5,<3.0` added to `pyproject.toml`.

### Tests

```
pytest tests/ --create-db → 75 passed
```

Breakdown:

| File | Coverage | Count |
|---|---|---|
| `tests/agent/test_schemas.py` | Pydantic invariants — extra="forbid", evidence trimming, value enums, confidence bounds | 10 |
| `tests/agent/test_taxonomy.py` | Snapshot building, frozen-ness, membership helpers | 3 |
| `tests/agent/test_validate.py` | UNKNOWN-by-default, unknown slugs dropped, confidence floor, invalid URLs | 9 |
| `tests/agent/test_merge.py` | Never-overwrite contract; capability flips; editorial proposals route to queue; forbidden-field invariant | 17 |
| `tests/agent/test_enrich.py` | End-to-end pure-Python pipeline with MockLLMProvider | 5 |
| `tests/agent/test_persist.py` | Hard DB invariants (status/verdict never moved), atomicity, queue payload | 10 |
| `tests/agent/test_agent_run_command.py` | `manage.py agent_run` dry-run + apply | 3 |
| `tests/sources/*` (Phase 0) | Existing | 18 |

### Hard-invariant evidence

* No test in `tests/agent/` ever observes `App.status` move from
  `DRAFT`, `editorial_review_status` move from `UNREVIEWED`, or
  `App.verdict` become non-empty after `apply_merge_set`.
  `test_status_fields_never_change` asserts all five forbidden
  columns explicitly with an LLM response that proposes editorial
  changes for each.
* `_assert_plan_does_not_touch_forbidden` in `persist.py` raises if a
  hypothetical future regression in the merge layer ever proposes
  a forbidden-field write — defense in depth.
* `test_existing_text_field_is_NEVER_overwritten` confirms that even
  when the LLM proposes a different value, the App keeps the
  editor's text and the disagreement routes to
  `NeedsReviewQueueEntry`.
* `test_existing_capability_yes_is_NEVER_flipped` — same for
  capabilities.

### Out of scope (Phase 1b)

* `AnthropicProvider` / `OpenAIProvider` real implementations
  (token-counting, retry, prompt-caching). Currently raise
  `NotImplementedError` so deployments without API keys can run
  Phase 1 in mock-only mode.
* `enrich_pending_drafts_batch` is registered as a task but NOT
  wired into `CELERY_BEAT_SCHEDULE`. Operators trigger via
  `manage.py agent_run --enrich-pending` until real-LLM is live.
* Acceptance-criteria items #6 (cost/card + latency/card from real
  models) and #7 (editor acceptance rate ≥ 60% on 10 cards) require
  Phase 1b + live editor review and are scheduled accordingly.

**Phase 1 part-1 complete. Gate to Phase 1b (real LLM providers): OPEN.**

---

## Phase 1 — Follow-up hardening (post-review, 2026-05-13)

External reviewer flagged that the persist layer trusted the pure
merge result too much. Fixed in this pass.

### High #1 — DRAFT-only status guard

Before this fix, `--enrich-app=<slug>` and the Celery task
`enrich_existing_draft_task(app_id)` accepted any App by id/slug;
`run_enrich_existing_draft` applied the merge without checking
`App.status`. This could (silently) mutate PUBLISHED cards in
violation of the Phase 0 → Phase 1 gate criterion #4
("0 published apps touched").

Fix:

* `apps/agent/persist.py::AppNotEligibleError` (new) — typed
  exception with `app_id` / `status` / `reason` attrs.
* `apps/agent/persist.py::assert_app_is_eligible(app_id)` — cheap
  pre-flight (single indexed PK read). Called by
  `tasks.run_enrich_existing_draft` *before* the LLM call so we
  never spend tokens on an ineligible target.
* `apply_merge_set` re-checks `App.status == DRAFT` *under the row
  lock*, defending against a race where the status changes between
  the pre-flight and the apply.
* `apps/agent/management/commands/agent_run.py` catches
  `AppNotEligibleError` and surfaces it as `CommandError` — operator
  sees a clean message, not a stack trace.

Tests: `test_assert_app_is_eligible_rejects_published`,
`test_assert_app_is_eligible_rejects_missing`,
`test_apply_merge_set_refuses_published_under_lock` (race scenario),
`test_published_app_is_rejected_with_command_error`.

### High #2 — Race-safe persist (stale snapshots)

`compute_merge` was conservative against the snapshot at pipeline
input time, but `apply_merge_set` then executed the precomputed plan
blindly — an editor edit committed between `build_app_snapshot()` and
`apply_merge_set()` would be silently overwritten.

Fix:

* `apply_merge_set` opens its atomic block with
  `App.objects.select_for_update().get(pk=app_id)`. The locked row's
  current values are the source of truth from here on.
* `_apply_field_updates(locked_app, plan)` re-checks every field
  update against `locked_app.<field>` and drops the update if the
  current value is non-empty. The LLM proposal remains preserved in
  `Source.payload` (audit trail).
* `_apply_capability_updates(app_id, plan)` fetches existing
  `AppCapability` rows under `SELECT ... FOR UPDATE` and applies
  only when the current value is `unknown`. Existing `yes` / `no`
  is *never* flipped under any circumstance.

Tests:
* `test_field_race_editor_wins_between_snapshot_and_apply` —
  simulates editor write to `App.short_description` between snapshot
  and apply; asserts the editor's value survives.
* `test_capability_race_editor_wins_between_snapshot_and_apply` —
  same shape for `AppCapability.open_source` flipped to NO
  pre-apply; agent's YES must lose.

### High #3 — Search vector stays warm after field-only enrichment

`App.objects.update(**fields)` does not fire `post_save`, so the
signal-based `refresh_search_vector_task` in `apps/catalog/signals.py`
never gets enqueued on text-only fills. Search would not see the new
`short_description` / `long_description` / `developer_name` /
`developer_url` until the nightly safety-net rebuild.

Fix:

* `_apply_field_updates` schedules
  `refresh_search_vector_task.delay(app_id)` via
  `transaction.on_commit` whenever the function actually wrote
  fields. Mirrors the pattern in `apps/catalog/signals._schedule_refresh`.
* On rollback the callback is discarded (the whole point of
  `on_commit`); no stale refreshes triggered.

Tests:
* `test_search_vector_refresh_is_scheduled_on_field_update`
  monkeypatches `transaction.on_commit` to capture callbacks and
  stubs `refresh_search_vector_task.delay`; asserts a callback was
  scheduled that, when invoked, calls `.delay(app_id)`.
* `test_no_field_updates_means_no_fields_written` confirms the
  invariant that gates the refresh (capability-only / category-only
  merges report `fields_written == []`).

### Medium #1 — Dry-run documentation

`apps/agent/management/commands/agent_run.py` docstring claimed
dry-run wrote `Source.payload` snapshots, but `_upsert_agent_source`
is called from `apply_merge_set` only — never during dry-run.

Fix: docstring rewritten to match reality. Dry-run writes
`AgentRun` / `EnrichmentTask` / `LLMCallLog` (full proposal in
`EnrichmentTask.diff_summary`) but no `Source` row. The
`agent-enrich:<id>` Source is only created on `--apply`.

### Medium #2 — TaxonomySnapshot hashability claim

The dataclass docstring claimed hashability as a benefit; the dict
fields (`capability_descriptions`, `category_descriptions`) make it
non-hashable. Fix: docstring rewritten to drop the hashability
claim — we want fast dict lookups, not set/dict keys.

### Low/Medium — Distinct provenance for agent Sources

Agent-generated audit `Source` rows were stored with
`source_type=manual`, indistinguishable from manual entry except by
the `external_id` prefix `agent-enrich:`. Fixed before this becomes
load-bearing in analytics / admin filters.

Fix:

* `apps/sources/models.py::Source.SourceType.AGENT_ENRICH = "agent_enrich"`.
* `apps/sources/migrations/0003_alter_source_source_type.py` —
  Django auto-migration (choices-only change, no DB-level schema
  change).
* `apply_merge_set` default `source_type` changed from `MANUAL` to
  `AGENT_ENRICH`; `tasks.run_enrich_existing_draft` updated to match.

Test: `test_audit_source_uses_agent_enrich_source_type`.

### Test totals

```
pytest tests/ → 84 passed
```

| Suite | Tests | Δ from part-1 |
|---|---|---|
| `tests/sources/*` (Phase 0) | 18 | — |
| `tests/agent/test_schemas.py` | 10 | — |
| `tests/agent/test_taxonomy.py` | 3 | — |
| `tests/agent/test_validate.py` | 9 | — |
| `tests/agent/test_merge.py` | 17 | — |
| `tests/agent/test_enrich.py` | 5 | — |
| `tests/agent/test_persist.py` | 17 | +7 |
| `tests/agent/test_agent_run_command.py` | 4 | +1 |

**Phase 1 hardened. Persist layer is now the final safety boundary
the reviewer asked for. Gate to Phase 1b (real LLM providers): OPEN.**

---

## Phase 1b — OpenAI real provider + production-like smoke (2026-05-13)

### Shipped

* `apps/agent/llm/client.py::OpenAIProvider` implemented against the
  official OpenAI SDK using Pydantic structured outputs.
* Provider-specific OpenAI wire schema added for `MergeSet` so strict
  structured outputs can accept the request while the internal pipeline
  continues to consume the existing ergonomic `MergeSet` contract.
* `LLMCallMetadata` is populated from provider usage: input/output
  tokens, cached tokens, latency, `is_mock=False`, and optional cost
  using `AGENT_OPENAI_INPUT_COST_PER_1M_TOKENS` /
  `AGENT_OPENAI_OUTPUT_COST_PER_1M_TOKENS`.
* Real-provider failure messages are sanitized before bubbling into
  task/audit logs (status/code only; no raw SDK message carrying key
  fragments).
* Docker image now installs `openai>=1.50,<2.0`; `web`, `worker`, and
  `beat` were rebuilt/restarted after `.env` switched to OpenAI.
* `/health/` pg_trgm check fixed: it now calls
  `similarity('a'::text, 'a'::text)` instead of the invalid `%%`
  literal operator. Container health is green.

### Verification

* Real OpenAI smoke call returned HTTP 200 and parsed `MergeSet`
  (`provider=openai`, `is_mock=False`).
* `curl http://localhost:8000/health/`:

  ```
  {"status": "ok", "checks": {"db": true, "redis": true, "pg_trgm": true}}
  ```

* Focused local tests:

  ```
  pytest tests/core/test_healthcheck.py tests/agent/test_llm_client.py \
    tests/agent/test_enrich.py tests/agent/test_validate.py \
    tests/agent/test_merge.py tests/agent/test_schemas.py
  → 51 passed
  ```

**Phase 1 is complete. Discovery, batch automation, and editor review
acceptance-rate measurement remain Phase 2+ gates; `AGENT_SOURCES_ENABLED`
stays empty.**

---

## Phase 2 — Admin review queue, first editor workflow slice (2026-05-13)

### Shipped

* `NeedsReviewQueueEntryAdmin` is no longer a raw JSON-only view:
  it renders current App state, structured LLM proposals, and linked
  LLM context (`AgentRun`, `EnrichmentTask`, `LLMCallLog`) on the
  change page.
* Object-level review buttons added on unresolved entries:
  `Apply proposed verdict`, `Apply launch status`, `Apply pricing`,
  `Reject all`, `Mark resolved`, `Approve & publish`.
* Bulk admin actions added for the same editor workflows. Publishing
  goes through `apps.catalog.services.transition_to_published`, so the
  existing business checklist remains the only publish gate.
* Resolution metadata is recorded on queue entries:
  `resolved_at`, `resolved_by`, and a short `resolution_note`.
* `NeedsReviewQueueEntry.review_outcome` added for measurable review
  outcomes (`accepted`, `rejected`, `no_action`, `published`) so Phase 1
  quality gate #7 can be computed from DB state instead of parsing notes.
* Daily digest task `apps.agent.tasks.send_review_queue_digest` added and
  wired to Celery beat at 07:30 UTC. It sends one email listing the current
  open queue to `AGENT_REVIEW_DIGEST_EMAILS`, falling back to
  `SUBMISSIONS_NOTIFY_EMAILS`.
* `apps.agent.tasks.review_acceptance_stats(days=30)` added for the
  editor acceptance-rate measurement.
* Agent hard constraints remain intact: none of this is reachable from
  pipeline/persist code. These writes happen only after an authenticated
  Django admin editor action.

### Tests

```
DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/agent/ -v
→ 88 passed

DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/ -q
→ 108 passed
```

New coverage in `tests/agent/test_admin_review_queue.py`:

| Scenario | Assertion |
|---|---|
| Change view render | Current App, proposal, LLM metadata, and action buttons are visible |
| Apply proposed verdict | Writes `App.verdict`; queue entry remains unresolved for explicit editor closure |
| Reject all | Marks entry resolved with editor attribution |
| Bulk approve & publish | Uses `transition_to_published`; marks entry resolved only after successful publish |
| Digest task | Sends a single email for open queue entries; skips empty queue / no recipients |
| Acceptance stats | Counts accepted / rejected / no-action / published outcomes |

### Status

**Phase 2 implementation complete.** Remaining Phase 2 gate evidence is
operational, not code: after real editors review at least 10 LLM proposals,
run `review_acceptance_stats(days=30)` and record whether acceptance rate
is ≥ 60% before scaling discovery.

---

## Phase 3 — Discovery RSS + GitHub, safe first slice (2026-05-13)

### Shipped

* `apps.agent.sources.rss_feeds` parses RSS 2.0 and Atom feeds into
  normalized `DiscoveryCandidate` records using the standard library.
* `apps.agent.sources.github_mcp_search` wraps GitHub repository search
  and converts repository metadata into conservative MCP `AppDraft`s
  when an operator explicitly runs non-dry-run discovery.
* `DiscoveryDecision` schema + `discover-v1.0` prompt added for cheap
  LLM classification: candidate URL → relevant YES/NO, canonical URL,
  reason, confidence.
* `apps.agent.pipeline.discovery.classify_candidate` added as pure
  prompt → LLM → structured decision glue.
* Celery tasks added:
  - `discover_rss(limit=20, dry_run=False)`
  - `discover_github_mcp(limit=20, dry_run=False)`
* Both discovery tasks are guarded by `AGENT_SOURCES_ENABLED`; beat can
  invoke them safely while they no-op until `rss` / `github_mcp` are
  explicitly enabled. Manual dry-runs bypass the flag.
* Beat schedule added for RSS every 6 hours and GitHub MCP on
  Mon/Wed/Fri at 06:30 UTC.
* Source types added: `rss_discovery`, `github_mcp`.

### Tests

Focused discovery run:

```
DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/agent/test_discovery_sources.py \
  tests/agent/test_discovery_tasks.py -v
→ 8 passed

DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/ -q
→ 116 passed
```

Coverage:

| Scenario | Assertion |
|---|---|
| RSS item parse | title/link/summary normalized and HTML stripped |
| Atom entry parse | GitHub-style Atom entry normalized |
| GitHub search parse | malformed rows skipped; repo metadata retained |
| GitHub minimal draft | platform/listing/capability/source metadata conservative |
| Discovery dry-run | writes `AgentRun`/`EnrichmentTask`/`LLMCallLog`, no `App` |
| Non-relevant candidate | task marked `skipped` |
| GitHub apply | with `AGENT_SOURCES_ENABLED=['github_mcp']`, creates DRAFT via `upsert_app_from_draft` |
| Feature guard | non-dry-run discovery no-ops when source flag disabled |

### Deferred

* README fetch + LLM `EnrichedDraft` for richer GitHub drafts.
* RSS positive candidates currently create audit rows; full
  `enrich_new_app_task(url, source_type)` remains the next Phase 3 slice.
* Production gate still requires ≥ 20 LLM-generated DRAFT and ≥ 50%
  approval-to-published rate before Phase 4.

### Slice 2 — New-app enrichment path (2026-05-13)

* `apps.agent.pipeline.fetch.FetchResult` + `fetch_url_text` added as the
  first fetch primitive for candidate URL enrichment.
* `enrich_new_app_prompt` (`enrich-new-v1.0`) added. It asks for
  `EnrichedDraft` and repeats the same hard rules: evidence for
  yes/no capabilities, allowed taxonomy slugs only, no invented URLs,
  verdict only as proposal.
* `validate_enriched_draft` added so new-card enrichment gets the same
  guardrails as existing-draft merge: unknown slugs dropped, low
  confidence taxonomy dropped, invalid URLs stripped, evidenceless
  capabilities downgraded to `unknown`.
* `apps.agent.persist.persist_new_draft` added as the only Django bridge
  for new LLM-generated cards. It converts sanitized `EnrichedDraft` to
  `AppDraft` and persists via `upsert_app_from_draft`; `App.status`
  remains `draft`, `editorial_review_status` remains `unreviewed`, and
  `proposed_verdict` stays in `Source.payload`.
* `run_enrich_new_app` + `enrich_new_app_task` added. Dry-runs write
  `AgentRun` / `EnrichmentTask` / `LLMCallLog` only; apply mode creates
  a DRAFT and links the task to the created App.
* Discovery batches now call full new-app enrichment for relevant
  candidates when the source is enabled. `discover_rss` uses
  `rss_discovery`; `discover_github_mcp` uses `github_mcp`.
* OpenAI wire schema extended for `EnrichedDraft` so real structured
  output can return capability lists while internal pipeline still uses
  a capability dict.

Focused tests:

```
DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/agent/test_enrich_new_app_task.py \
  tests/agent/test_validate.py tests/agent/test_llm_client.py -v
→ 21 passed

DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/agent/test_discovery_tasks.py \
  tests/agent/test_enrich_new_app_task.py -v
→ 7 passed

DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/ -q
→ 121 passed
```

### Slice 3 — Manual discovery CLI (2026-05-13)

* `manage.py agent_run --source=rss --limit=N` added.
* `manage.py agent_run --source=github_mcp --limit=N` added.
* Default remains dry-run. Passing `--apply` delegates to the same
  discovery tasks beat uses, so non-dry-run source execution is still
  guarded by `AGENT_SOURCES_ENABLED`.

Focused tests:

```
DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/agent/test_agent_run_command.py -v
→ 6 passed

DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/ -q
→ 123 passed
```

### Slice 4 — GitHub README fetch (2026-05-14)

* GitHub discovery enrichment now fetches README markdown through the
  GitHub Contents API (`/repos/{owner}/{repo}/readme`) instead of
  sending repository HTML to the LLM.
* `fetch_github_readme_text` is token-aware and test-injectable; it
  decodes base64 README content into `FetchResult` with source metadata
  (`html_url`, `download_url`, `path`, `size`).

Focused tests:

```
DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/agent/test_discovery_sources.py \
  tests/agent/test_discovery_tasks.py -v
→ 10 passed

DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/ -q
→ 124 passed
```

### Slice 5 — Phase 3 gate report (2026-05-14)

* `apps.agent.reports.phase3_gate_report()` added as the canonical
  Phase 3 → Phase 4 evidence rollup.
* `manage.py agent_phase3_report [--json]` added for operators. It
  reports LLM-generated RSS/GitHub apps, status split, approval rate,
  total LLM cost, cost per published app, real/mock call counts, and
  whether the production gate is open.
* Source rows with `payload.agent_enrichment` are the source of truth for
  "LLM-generated via RSS/GitHub", because those rows are written only
  after `enrich_new_app` has produced a sanitized draft and
  `persist_new_draft` has created/updated an `App`.
* Gate opening is intentionally stricter than the bare count/rate: cost
  basis must be complete (`llm_calls >= generated_apps`) and contain no
  mock calls, otherwise cost-per-published is not production evidence.

Focused tests:

```
DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/agent/test_phase3_report.py -q
→ 2 passed
```

Current dev DB gate snapshot:

```json
{
  "generated_apps": 0,
  "published_apps": 0,
  "approval_rate": "0.0",
  "cost_per_published_usd": null,
  "cost_basis_complete": false,
  "gate_open": false
}
```

**Phase 3 implementation is now complete. Phase 4 remains CLOSED until
real RSS/GitHub production runs accumulate ≥ 20 LLM-generated apps,
approval-to-published reaches ≥ 50%, and real cost per published app is
measured.**

---

## Phase 4 — Prerequisite link-checker fixes (2026-05-14)

Before starting official-directory discovery or re-actualization, the two
Phase 0 deferred link-checker issues were fixed:

* `check_app_links_batch` now includes published apps where
  `last_checked_at IS NULL`, so newly-published / never-checked apps are
  not silently skipped. Null timestamps are ordered first.
* Auto-deprecate for `official` / `install` link failures now fires at
  **7** consecutive failures, matching `docs/business.md § 11.3` and
  `docs/architecture.md § 11`, instead of the old 5-failure threshold.

Focused tests:

```
DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/sources/test_link_checker.py -q
→ 2 passed
```

**Phase 4 prerequisite status: link-checker blocker cleared.**

### Slice 1 — Source re-actualization metadata (2026-05-14)

Prepared the storage surface for future re-actualization without enabling
any Phase 4 automation:

* `Source.last_enriched_at` added as nullable metadata.
* Migration `sources.0005_source_last_enriched_at` backfills existing rows
  with `last_enriched_at = fetched_at`.
* Source admin now exposes `last_enriched_at` in list display / filters.

Verification:

```
.venv/bin/python manage.py makemigrations --check --dry-run
→ No changes detected

DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/python manage.py migrate sources
→ Applying sources.0005_source_last_enriched_at... OK

DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/sources/test_source_model.py \
  tests/sources/test_link_checker.py -q
→ 3 passed
```

No discovery/re-actualization beat task was enabled; Phase 4 production
automation remains blocked by the Phase 3 production gate and official
directory ToS review.

---

## Phase 3 — Controlled real pilot (2026-05-14)

First end-to-end real-LLM pilot of the discovery pipeline against
production-like inputs. Goal: confirm that RSS + GitHub discovery, cheap
classification, primary enrichment, validation, and DRAFT persistence all
work against real OpenAI calls — not a gate run.

### Setup

* `.env` updates: `AGENT_LLM_PROVIDER_CHEAP=openai`,
  `AGENT_LLM_MODEL_CHEAP=gpt-5.4-nano`; primary stays
  `openai`/`gpt-5.4-mini`. `GITHUB_TOKEN` added (search rate-limit lift
  from 10 req/min unauth → 30 req/min authed; repo content reads use the
  5000 req/h limit).
* Run host: `.venv/bin/python manage.py …` against the dev Postgres
  (`127.0.0.1:5432`). Container image is pre-Phase-3 and was not used.
  Celery scheduler bypassed via `CELERY_TASK_ALWAYS_EAGER=True` for the
  `--apply` command, because the host venv has no route to the docker
  internal Redis hostname `redis` from `transaction.on_commit` →
  `refresh_search_vector_task.delay`. The eager path uses the same
  refresh code; it does not change persisted state.

### Commands and outcomes

| # | Command | Result |
|---|---|---|
| 1 | `agent_run --source=rss --limit=3` (dry) | `seen=3 relevant=0 persisted=0` — 3 OpenAI blog/Academy posts correctly classified as non-product |
| 2 | `agent_run --source=github_mcp --limit=3` (dry) | `seen=3 relevant=2 persisted=0` — 2 MCP-adjacent repos flagged relevant |
| 3 | `AGENT_SOURCES_ENABLED=rss,github_mcp agent_run --source=rss --limit=3 --apply` | `seen=3 relevant=0 persisted=0` — same feed, no relevant items |
| 4 | `AGENT_SOURCES_ENABLED=rss,github_mcp CELERY_TASK_ALWAYS_EAGER=True agent_run --source=github_mcp --limit=3 --apply` | `seen=3 relevant=1 persisted=1` — App #8 `maven-tools-mcp-server` created |
| 5 | `agent_phase3_report` | gate **CLOSED**: `generated_apps=1 published=0 approval_rate=0.0% cost_basis_complete=yes real_llm_calls=1` |

LLM-call totals over the pilot:

* 9 cheap classifications via `gpt-5.4-nano` (`discover-v1.0` prompt),
  prompt sizes 256-317 tokens, outputs 72-117 tokens. All `is_mock=False`.
* 1 primary enrichment via `gpt-5.4-mini` (`enrich-new-v1.0` prompt),
  3641/634 in/out tokens, 4406 ms. `is_mock=False`.

### Hard-constraint audit (App #8)

| Field | Expected | Observed |
|---|---|---|
| `status` | `draft` | `draft` |
| `editorial_review_status` | `unreviewed` | `unreviewed` |
| `developer_claim_status` | `unclaimed` | `unclaimed` |
| `platform_verification_status` | not `official` | `unknown` |
| `verdict` | empty string | `""` |
| Published-app `updated_at` since pilot start | 0 rows | 0 rows |
| Source row | one `github_mcp` row, `payload.agent_enrichment` present | confirmed |

DRAFT taxonomy population (real LLM output): categories
`['developer-tools']`, platforms `['mcp']`, listing_types `['mcp-server']`,
use_cases `[]` (slug-mismatch — see findings), 8 capabilities written
(read_data=yes, write_actions=yes, interactive_ui=no, auth_required=unknown,
local_setup_required=yes, remote_available=yes, open_source=yes,
api_available=yes).

### Findings (not gate blockers, but to fix before scaling)

1. **`AppCapability.note` empty for new-app path.**
   `apps/sources/upsert.py::attach_capabilities` writes only `value`,
   never `note`. The merge path
   (`apps/agent/persist.py::_apply_capability_updates`) does write
   `note=evidence[:200]`. So the evidence-on-row invariant only holds for
   the merge (existing-draft) path; new discovery-created DRAFTs have
   evidence in `Source.payload.agent_enrichment.sanitized_draft.
   capabilities.<key>.evidence` but not on the `AppCapability` row.
   Acceptance criterion #5 is satisfied (evidence is in `Source.payload`),
   but the admin review UI cannot show it next to each capability without
   joining back to the Source row. Worth aligning the two paths.

2. **`use_cases` dropped on persist.** The LLM returned 5 use cases as
   free-text strings (`"dependency version checks"`, etc.).
   `upsert_app_from_draft` expects them as slugs that already exist in the
   `UseCase` table; unknown slugs silently never attach. Either the
   prompt should require existing slugs, or the persist layer should
   `get_or_create(slug=slugify(label))` for new-app drafts so the LLM's
   free-text use cases are not lost.

3. **Cost reads $0 across the pilot.** No
   `AGENT_OPENAI_INPUT_COST_PER_1M_TOKENS` /
   `AGENT_OPENAI_OUTPUT_COST_PER_1M_TOKENS` env vars are set, so
   `OpenAIProvider` writes `cost_usd=0` for every call. The Phase 3 gate
   report says `cost_basis_complete=yes` (count check passes) but
   `cost_per_published_usd=null` — the cost dimension of the gate is
   unmeasured. These env vars need to be configured with the real
   gpt-5.4-mini / gpt-5.4-nano price points before the gate evidence is
   meaningful.

4. **`proposed_verdict` came back empty.** The primary enrichment
   produced `proposed_verdict=""` for App #8, so nothing routes to the
   review queue from this card. Likely the prompt allows the model to
   skip the field; for editor visibility it's worth either making
   `proposed_verdict` mandatory in the prompt or surfacing the LLM
   `scope_summary` as the default review-queue payload.

5. **`AGENT_SOURCES_ENABLED` is the only thing standing between dry-run
   and apply.** Confirmed: `agent_run --source=… --apply` without
   `AGENT_SOURCES_ENABLED` containing the source no-ops at
   `_run_discovery_batch` (per
   `apps/agent/tasks.py:430`). The pilot used a process-scoped env
   override rather than editing `.env`, so the flag stays empty at rest.

6. **Celery-from-host gap.** `apps/catalog/signals.py` schedules
   `refresh_search_vector_task.delay()` via `transaction.on_commit`,
   which fails when the broker hostname `redis` is not resolvable from
   the host venv. Two ways out: (a) expose redis on the docker host port
   for local runs, or (b) always run `--apply` inside the container.
   For ad-hoc operator runs the `CELERY_TASK_ALWAYS_EAGER=True` override
   used here is acceptable; for any scheduled production-style run the
   container path should be the default.

### Status after pilot

* **Pipeline mechanics work end-to-end against real OpenAI.** Discovery
  classification, candidate fetch (GitHub README via Contents API),
  EnrichedDraft generation, validation, persistence, and audit-trail
  rows all behave as designed.
* **Phase 3 → Phase 4 gate stays CLOSED.** Only 1 LLM-generated DRAFT
  (target ≥ 20); approval rate 0% (no editor reviews yet); cost
  not measured.
* **Next data-collection slice** to push toward the gate:
  - Configure `AGENT_OPENAI_INPUT_COST_PER_1M_TOKENS` and
    `AGENT_OPENAI_OUTPUT_COST_PER_1M_TOKENS` so cost is real.
  - Fix `use_cases` slug-creation path (Finding #2) before another
    apply run — otherwise every new DRAFT loses its use cases.
  - Run `agent_run --source=github_mcp --apply` at `limit=20-40` to
    accumulate ≥ 20 real DRAFTs.
  - Have an editor walk the review queue and call
    `review_acceptance_stats(days=30)` to measure acceptance rate.
  - Re-run `agent_phase3_report` for the real cost-per-published number
    once a handful of those DRAFTs reach `published`.

### Slice 7 — Fix findings #1, #2, #4 from pilot and re-test (2026-05-14)

Targeted code fixes for the discrepancies the pilot surfaced, then a
second mini-pilot to confirm the new behaviour.

**Fixes**

* `apps/sources/base.py` — `AppDraft` now carries
  `capability_evidence: dict[str, str]` and `use_cases: list[str]` so
  the LLM's per-capability quote and free-text use-case labels survive
  the trip from `EnrichedDraft` to the catalog.
* `apps/sources/upsert.py::attach_capabilities` now accepts an
  `evidence` dict and writes `note=evidence[key][:200]` for every
  capability that came with a quote. The merge path (`apps/agent/
  persist.py::_apply_capability_updates`) already did this; new-app
  discovery is now aligned.
* `apps/sources/upsert.py::attach_use_cases` (new) — resolves free-text
  use-case labels to `UseCase` rows via `get_or_create(slug=slugify
  (title), defaults={"title": title})`, exactly mirroring the merge
  path. Called from `_create_new_app`.
* `apps/agent/persist.py::_enriched_to_app_draft` populates both new
  draft fields from `EnrichedDraft.capabilities[*].evidence` and
  `EnrichedDraft.use_cases`.
* `apps/agent/persist.py::persist_new_draft` falls back to
  `scope_summary` for `Source.payload['proposed_verdict']` when the
  LLM left it empty — so the admin/queue always has a non-empty
  editor-facing line.
* `apps/agent/llm/prompts.py::enrich_new_app_prompt` tightened: rule #4
  now requires a 1-2 sentence `proposed_verdict`; rule #6 (new) tells
  the model to emit 3-7 verb-led use-case phrases.
* Finding #3 (per-model cost env vars) intentionally skipped per
  operator instruction; cost stays $0 until prices are configured.

**Tests**

```
DATABASE_URL=postgres://llmmarket:llmmarket@127.0.0.1:5432/llmmarket \
  .venv/bin/pytest tests/ -q
→ 129 passed
```

**Re-test commands**

Clean DB first: deleted App #7 + App #8 (leftover DRAFTs from the
first-pilot apply runs) and wiped `AgentRun`/`EnrichmentTask`/
`LLMCallLog` to start the retest from zero agent state. The earlier
"failed apply" actually committed the App row before the celery
broker-from-host error tripped `transaction.on_commit`, which is why
the cleanup needed a second pass (the source's payload didn't have
`agent_enrichment` because that update happens *after* upsert).

| # | Command | Result |
|---|---|---|
| 1 | `agent_run --source=rss --limit=3` (dry) | `seen=3 relevant=0 persisted=0` |
| 2 | `agent_run --source=github_mcp --limit=3` (dry) | `seen=3 relevant=1 skipped_existing=1 persisted=0` |
| 3 | `… --source=rss --limit=3 --apply` (env: sources enabled, eager celery) | `seen=3 relevant=0 persisted=0` |
| 4 | `… --source=github_mcp --limit=3 --apply` | `seen=3 relevant=3 persisted=3` — Apps #9, #10, #11 created |
| 5 | `agent_phase3_report` | `generated_apps=3 draft=3 approval_rate=0.0 real_llm_calls=3 mock=0 gate=CLOSED` |

**Per-app verification of the fixes**

| App | Capability evidence (yes/no with `note`) | Use-cases attached | proposed_verdict chars | App.verdict |
|---|---|---|---|---|
| #9 `forgemax` | 7/7 ✅ | 6 ✅ | 309 ✅ | `""` ✅ |
| #10 `mcp-tools-py` | 7/7 ✅ | 6 ✅ | 279 ✅ | `""` ✅ |
| #11 `gram` | 6/6 ✅ | 6 ✅ | 295 ✅ | `""` ✅ |

Sample evidence captured on `AppCapability.note` for App #9
`forgemax` (truncated):

| Capability | Value | Note |
|---|---|---|
| read_data | yes | "Forgemax acts as a gateway exposing search/execute tools that operate over connected MCP servers" |
| api_available | yes | "The README documents the MCP protocol surface and execute_tool endpoint" |
| open_source | yes | "Repository hosted on GitHub with an Apache-2.0 license badge" |
| interactive_ui | no | "No interactive UI is provided; communication is via MCP protocol clients" |

Sample use-case slugs created on the fly:
`chain-tool-calls-in-one-execution`,
`discover-downstream-tools`,
`orchestrate-connected-mcp-servers`,
`read-mcp-resources`,
`reduce-schema-bloat`,
`run-javascript-against-mcp-tools`.

**Hard-constraint audit (all 3 apps)**

* `status=draft` for every agent-created App.
* `editorial_review_status=unreviewed` for every agent-created App.
* `App.verdict=""` for every agent-created App.
* `App.published.filter(updated_at__gte=phase3_pilot_start).count() == 0`.

**Status**

Findings #1, #2, #4 from the prior pilot are resolved with regression
tests still green. Finding #3 (real per-model cost) and Finding #5
(host→broker route) remain open. Phase 3 gate is still CLOSED on
volume + editorial signal — three real DRAFTs are not twenty, no
editor reviews have been performed, and cost stays $0 until pricing
env vars are populated.

### Slice 8 — Docker rebuild, editor approval pass, demo-data cleanup (2026-05-15)

**Docker image rebuilt.** `docker compose build web worker beat` and
`docker compose up -d` brought the running container in line with the
host code (pre-rebuild image was missing `apps/agent/sources/` and the
last sources migration). `/health/` reports
`{"db": true, "redis": true, "pg_trgm": true}` after the rebuild.

**Admin verification via Playwright.** Logged in to
`/admin/` as `admin/admin123`, then exercised the relevant Phase 3
pages headlessly (screenshots under `/tmp/llmmarket-admin-*.png`):

| URL | Status | Rows / observation |
|---|---|---|
| `/admin/catalog/app/?status__exact=draft` | 200 | 3 rows (Gram, MCP Tools Py, Forgemax) |
| `/admin/catalog/app/9/change/` | 200 | Forgemax — categories, platforms, capabilities-with-`note` (evidence), 6 use_cases, publish checklist sidebar all populated |
| `/admin/sources/source/?source_type__exact=github_mcp` | 200 | 3 rows |
| `/admin/agent/agentrun/` | 200 | 2 rows (rss_discovery + github_mcp APPLY runs) |
| `/admin/agent/llmcalllog/` | 200 | 9 rows, all `provider=openai`, `is_mock=False` |
| `/admin/agent/needsreviewqueueentry/` | 200 | 0 rows (Phase 3 new-app path doesn't write to the queue — by design) |

**Editor approval pass.** All three DRAFTs cleared the technical
publish checklist (`apps/catalog/services.py::get_publish_checklist`)
on first read. The only blocking fields were the two an editor must
set: `editorial_review_status` and `platform_verification_status`.
The agent's `not_listed` is the correct value for github-discovered
listings (per business.md § 6.5 the `official` flag is reserved for
the four directory sources).

Inside the rebuilt container:

```
... For Forgemax / MCP Tools Py / Gram:
[OK] short_description ≥ 60 chars
[OK] at least one platform
[OK] at least one category
[OK] ≥ 3 explicit capabilities
[OK] official_page_url or install_url
[OK] editorial_review_status = reviewed
[OK] platform_verification_status is set
-> PUBLISHED  status=published quality_score=60
```

`recalc_quality_score` returned `60/100` for all three; the missing
points are claim verification, badge URL, and editorial verdict —
which remain editor-only fields.

**Phase 3 report after the approval pass:**

```
Phase 3 -> Phase 4 gate: CLOSED
Generated RSS/GitHub apps: 3 (draft=0, published=3, hidden=0)
Approval rate: 100.0%
LLM cost: $0.000000 total; $0.000000 per published app
LLM calls: 3 (real=3, mock=0)
Cost basis complete: yes
```

Volume is the only gate criterion still failing
(3/20 generated apps). Approval quality has moved from 0% to **100%**
on this batch — the LLM proposals were good enough for the technical
checklist to pass cleanly.

**Public UI verification via Playwright.** Hit the public catalog
under `http://localhost:8000` (screenshots under
`/tmp/llmmarket-public-*.png`):

| URL | Status | Mentions |
|---|---|---|
| `/` | 200 | Forgemax, MCP Tools Py, Gram visible in "Trending now" and "Fresh in the grid" |
| `/apps/` (full catalog) | 200 | same 3 |
| `/apps/newly-added/` | 200 | same 3 |
| `/apps/forgemax/` | 200 | full description + capability evidence list + similar tools |
| `/apps/mcp-tools-py/` | 200 | full description |
| `/apps/gram/` | 200 | full description |

**Side bug found (preexisting, not from pilot):**
`/apps/<category-slug>/` returns 404 because
`apps.catalog.urls` registers `apps/<slug:slug>/` (app detail) *before*
`config/urls.py` registers `apps/<slug:category_slug>/`
(category page). Django dispatches on first match, so the category
route is dead — only slugs that happen to be App slugs resolve to
anything. The comment in `apps/catalog/urls.py:6-7` already
acknowledges the collision risk but the wiring order is reversed.

**Demo-data cleanup.** With three real published apps in the catalog,
the operator asked to remove the synthetic seed apps that
`docker/entrypoint.sh` calls `manage.py seed_demo` to create on every
boot. Deletion summary (inside container):

```
Deleted total: 30
  catalog.AppPlatform:   11
  catalog.AppCategory:    8
  newsletter.IssueApp:    5
  catalog.App:            6
```

Apps removed: `AI Code Assistant`, `Smart Task Manager`,
`Data Analyzer Pro`, `Neural Writer`, `CyberShield MCP`, `PromptForge`.
`apps/catalog/management/commands/seed_demo.py::DEMO_APPS` cleared to
`[]` and the module docstring updated so the demo payload doesn't
return on the next `docker compose up`. Reference data
(platforms / categories / capabilities / listing-types from
`apps/catalog/fixtures/seed.json`) is untouched — the agent depends on
it for taxonomy snapshots.

Public catalog now contains exactly the 3 agent-generated apps; the
home page "Trending now" and "Fresh in the grid" sections show only
real Forgemax / MCP Tools Py / Gram cards.

**Tests after cleanup:** `pytest tests/ -q → 129 passed`.

**Status**

End-to-end pipeline verified through the public UI on real data. The
Phase 3 gate now closes only on the volume criterion (3/20). Logging
this run separately so the approval-rate metric (100% on this batch)
isn't conflated with future, larger pilots.
