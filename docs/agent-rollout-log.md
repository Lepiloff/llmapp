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
