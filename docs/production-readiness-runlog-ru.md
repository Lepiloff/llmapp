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

