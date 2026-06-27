# Production readiness runlog

Дата: 2026-06-26.

## Stage 2 — operational checks

Production commit: `f83da27`.

Safety flags:

- `AGENT_SOURCES_ENABLED=` empty.
- `AGENT_ENRICH_PENDING_SOURCE_TYPES=gemini_extensions,claude_connectors,chatgpt_unofficial`.
- `AGENT_REACTUALIZATION_ENABLED=False`.
- Budget: `$3.379525 / $20`, no discovery or hard-stop latch.

Checks:

- `/health/`: ok, DB/Redis/pg_trgm/Celery worker all true.
- `pg_backup`: daily dumps present, retention deleting old dumps; latest observed
  dump `llmmarket-20260625T205818Z.sql.gz`, size `9.2M`.
- `rebuild_sitemap`: success.
- `refresh_search_vectors_batch(batch_size=10)`: processed 0 because no
  published apps yet.
- `recalc_quality_scores_batch(batch_size=10)`: processed 0 because no
  published apps yet.
- `update_popular_searches`: updated 0, created 0.
- `generate_seo_reports`: ok, published apps 0.
- `check_app_links_batch(batch_size=5)`: checked 0 because no published apps.
- `agent_budget_check`: ok, no alerts sent.
- Retention count-only checks: all old-row counts 0.

Fix applied during Stage 2:

- `django_site` was still `example.com`, so sitemap used
  `https://example.com/...`.
- Updated `Site(id=1)` to `domain=llmappmarket.com`, `name=LLM App Market`.
- Invalidated sitemap cache; public sitemap now emits
  `https://llmappmarket.com/...`.
- Follow-up code fix: `docker/bootstrap.py` now keeps `django_site` synced from
  `SITE_BASE_URL` on container bootstrap.

## Stage 3 — direct-ingest pilot

Limited pilot size: 10 records per source.

Before:

- `LLMCallLog` count: 1287.
- Sources:
  - `chatgpt_unofficial`: 293
  - `mcp_registry`: 13908
  - `gemini_extensions`: 1050
  - `agent_enrich`: 1282
  - `claude_connectors`: 24

Dry-runs:

- Gemini: `seen=10`.
- Claude: `seen=10`.
- ChatGPT unofficial: `seen=10`.
- MCP Registry limited dry-run: `seen=10`, `unparsed=0`.

Applied first pass:

- Gemini: `seen=10`, `new=1`, `updated=9`, `failed=0`.
- Claude: `seen=10`, `new=1`, `updated=9`, `failed=0`.
- ChatGPT unofficial: `seen=10`, `new=0`, `updated=10`, `failed=0`.
- MCP Registry: `seen=10`, `new=0`, `updated=10`, `failed=0`, `unparsed=0`.

Applied second pass:

- Gemini: `new=0`, `updated=10`, `failed=0`.
- Claude: `new=0`, `updated=10`, `failed=0`.
- ChatGPT unofficial: `new=0`, `updated=10`, `failed=0`.
- MCP Registry: `new=0`, `updated=10`, `failed=0`, `unparsed=0`.

After:

- `LLMCallLog` count: 1287, delta 0.
- Sources:
  - `gemini_extensions`: 1051
  - `claude_connectors`: 25
  - `chatgpt_unofficial`: 293
  - `mcp_registry`: 13908
  - `agent_enrich`: 1282

Conclusion:

- Direct-ingest sources can run without LLM calls.
- Limited repeated runs did not create duplicate new rows.
- Non-MCP direct-ingest found 2 genuinely new entries in the first 10-record
  pilot.

## Stage 5 — editorial automation policy implementation

Implemented a conservative non-MCP autopublish command:

```bash
python manage.py autopublish_candidates --limit 100
```

Default behavior is dry-run. It evaluates draft apps from:

- `gemini_extensions`
- `claude_connectors`
- `chatgpt_unofficial`

Safety rules:

- MCP is excluded unless the operator passes both
  `--source-type=mcp_registry` and `--include-mcp`.
- Final publication still goes through
  `apps.catalog.services.transition_to_published`.
- Pending duplicate candidates block publication.
- Pending review entries block publication unless the entry is a safe
  enrichment proposal:
  - no skipped field/capability updates;
  - no deprecated launch status;
  - no overwrite of non-empty editorial fields;
  - high-information verdict only;
  - no conflicting proposals across multiple pending entries.
- The command may auto-resolve safe enrichment entries, set
  `editorial_review_status=reviewed`, and publish only with explicit
  `--apply`.

Pilot apply shape:

```bash
python manage.py autopublish_candidates --limit 25 --apply
```

Expected validation after deploy:

- dry-run report has `evaluated`, `would_publish`, `blocker_counts`;
- apply run publishes only the candidates that passed the dry-run policy;
- published pages appear in UI/API/sitemap;
- no LLM calls are created by the autopublish command.

Production dry-run on commit `4738ab2`:

- `LLMCallLog`: `1287 -> 1287`, no LLM calls.
- Evaluated non-MCP candidates: `1064`.
- Initial `would_publish`: `1`.
- The single candidate was `Lucid`, but inspection showed it had both
  `chatgpt_unofficial` and `mcp_registry` sources and `mcp-server` listing
  type. Apply was not run.
- Follow-up safety fix: non-MCP autopublish now blocks any app that already
  has an `mcp_registry` source unless MCP is explicitly opted in.

## Stage 5 follow-up — trusted platform verification

The first autopublish dry-run showed `platform_verification_unknown` as the
dominant blocker (`1052/1064` evaluated non-MCP candidates). The original
upsert policy only marked MCP Registry cards as `official`, which was too
conservative for official direct directories.

Implemented:

- Future ingest marks `platform_verification_status=official` for:
  - Claude Connector rows whose directory URL is `https://claude.com/connectors...`;
  - ChatGPT rows whose directory URL is `https://chatgpt.com/apps...`.
- Gemini Extensions remain `unknown` by default because the current source is
  GitHub/list-style data and many rows are mixed with MCP.
- Existing rows can be updated with:

```bash
python manage.py backfill_trusted_platform_verification --limit 500
python manage.py backfill_trusted_platform_verification --limit 500 --apply
```

Safety rules:

- dry-run by default;
- MCP-mixed apps are excluded unless `--include-mcp` is passed;
- only `unknown -> official` transitions are written;
- no publish and no LLM calls happen in this command.
