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

Production run on commit `00e725b`:

- `/health/`: ok.
- `LLMCallLog`: `1287 -> 1287`, no LLM calls.
- Trust dry-run: `evaluated=295`, `would_update=295`.
- Trust apply: `updated=295`.
- Follow-up autopublish dry-run:
  - `evaluated=1064`;
  - `would_publish=1`;
  - `published=0`.
- The single candidate was `Figma`, but inspection showed it had MCP taxonomy
  (`platform=mcp`, `listing_type=mcp-server`) even without an `mcp_registry`
  source. Apply was not run.
- Follow-up safety fix: non-MCP autopublish now blocks MCP platform/listing
  type too; trust backfill also excludes MCP platform/listing type by default.

Production dry-run on commit `dbb23f2`:

- `/health/`: ok.
- `LLMCallLog`: `1287 -> 1287`, no LLM calls.
- Autopublish dry-run:
  - `evaluated=1064`;
  - `would_publish=0`;
  - `published=0`.
- Top remaining blockers for Claude/ChatGPT-only dry-run:
  - `short_description_lt_60=298`;
  - `verdict_required=192`;
  - `low_information_verdict=187`;
  - `explicit_capabilities_lt_3=118`.

Follow-up implementation:

- Added a compact publish profile for trusted non-MCP cloud connectors:
  - applies only when the app is an official Claude/ChatGPT directory row;
  - excludes MCP source, MCP platform, and `mcp-server` listing type;
  - accepts `short_description >= 20` when `long_description >= 60`;
  - accepts at least 2 explicit capabilities instead of 3.
- Autopublish no longer treats a low-information proposed verdict as a blocker;
  it simply does not apply that verdict. The base publish gate never required
  verdict, so this aligns automation with the checklist.

Production run on commit `e11782a`:

- `/health/`: ok.
- `LLMCallLog`: `1287 -> 1287`, no LLM calls.
- Autopublish dry-run after compact profile:
  - `evaluated=1064`;
  - `would_publish=170`;
  - `published=0`.
- Claude-only dry-run:
  - `evaluated=25`;
  - `would_publish=14`.
- First applied pilot:

```bash
python manage.py autopublish_candidates --source-type claude_connectors --limit 5 --apply
```

Result:

- `evaluated=5`;
- `would_publish=4`;
- `published=4`;
- `explicit_capabilities_lt_2=1` remained blocked (`actively`);
- `LLMCallLog`: `1287 -> 1287`;
- `App.status` counts after pilot: `draft=15031`, `published=4`.

Published apps:

- `10x-genomics-cloud`
- `activecampaign`
- `adisinsight`
- `adobe-cja`

Validation:

- All 4 public pages returned HTTP 200.
- Public API `GET /api/v1/apps/?platform=claude&page_size=10` returned
  `count=4`.
- Public sitemap includes all 4 app URLs with `llmappmarket.com`.
- `/search/?q=adobe` and `/claude-connectors/` returned HTTP 200.

## Stage 5 follow-up — second Claude autopublish batch

Production run on commit `9850a52`:

- Deployed verdict-prefix safety fix:
  - verdict values starting with `PROPOSAL:`, `PROPOSED:`, or `DRAFT:` are
    ignored instead of written to public `App.verdict`.
- `/health/`: ok.
- `LLMCallLog`: `1287 -> 1287`, no LLM calls.
- Claude-only dry-run before apply:
  - `published_count=4`;
  - `evaluated=21`;
  - `would_publish=10`.
- Applied second Claude batch:

```bash
python manage.py autopublish_candidates --source-type claude_connectors --limit 50 --apply
```

Result:

- `published_before=4`;
- `published_after=14`;
- `published=10`;
- `LLMCallLog`: `1287 -> 1287`.

Newly published apps:

- `adobe-experience-manager`
- `adobe-creativity`
- `adobe-marketing-agent`
- `ahrefs`
- `airops`
- `aiwyn-tax`
- `apollo`
- `asana`
- `attio`
- `adobe-workfront`

Validation:

- All 10 new public pages returned HTTP 200.
- Public API `GET /api/v1/apps/?platform=claude&page_size=20` returned
  `count=14`.
- Public sitemap includes the new app URLs.

Remaining Claude blockers after the second batch:

- `actively`: `explicit_capabilities_lt_2`
- `adobe-journey-optimizer`: MCP taxonomy + short description/capability +
  launch-status review change
- `aiera`: `pending_duplicate_candidate`
- `airtable`: `pending_duplicate_candidate`
- `airwallex`: `pending_duplicate_candidate`
- `alltrails`: `short_description_lt_20`
- `alma`: `category_required`, `explicit_capabilities_lt_2`
- `amplitude`: MCP source/platform + short description
- `atlassian`: `pending_duplicate_candidate`
- `audible`: `explicit_capabilities_lt_2`, `pending_duplicate_candidate`
- `aura`: `pending_duplicate_candidate`

## Stage 5 follow-up — trusted connector capability backfill

Production run on commit `186cd4b`:

- Deployed `backfill_trusted_connector_capabilities`.
- Runtime checks:
  - `python manage.py check`: ok;
  - `/health/`: ok;
  - new management command available inside the `web` container.
- Claude-only capability backfill dry-run:
  - `evaluated=23`;
  - `would_update=22`.
- Applied capability backfill:

```bash
python manage.py backfill_trusted_connector_capabilities \
  --source-type claude_connectors --limit 100 --apply
```

Result:

- `updated=22`;
- `updated_capabilities=38`;
- `LLMCallLog`: unchanged at `1287`.

Follow-up Claude autopublish dry-run:

- `evaluated=11`;
- `would_publish=1`;
- candidate: `actively`.

Applied autopublish:

```bash
python manage.py autopublish_candidates \
  --source-type claude_connectors --limit 50 --apply
```

Result:

- `published=1`;
- newly published app: `actively`;
- `App.status` counts after run: `draft=15185`, `published=15`;
- Claude published count: `15`;
- `LLMCallLog`: unchanged at `1287`.

Validation:

- Public page `https://llmappmarket.com/apps/actively/` returned HTTP 200.
- Public API `GET /api/v1/apps/?platform=claude&page_size=20` returned
  `count=15` and includes `actively`.
- Public sitemap includes `https://llmappmarket.com/apps/actively/`.

Remaining Claude blockers after this run:

- `adobe-journey-optimizer`: MCP taxonomy + short description/capability +
  launch-status review change
- `aiera`: `pending_duplicate_candidate`
- `airtable`: `pending_duplicate_candidate`
- `airwallex`: `pending_duplicate_candidate`
- `alltrails`: `short_description_lt_20`
- `alma`: `category_required`
- `amplitude`: MCP source/platform + short description
- `atlassian`: `pending_duplicate_candidate`
- `audible`: `pending_duplicate_candidate`
- `aura`: `pending_duplicate_candidate`

## Stage 5 follow-up — directory-host duplicate repair

Production run on commit `c65294b`:

- Deployed weak duplicate guardrail:
  - shared directory hosts such as `claude.com` and `chatgpt.com` no longer
    create `shared_domain_similar_name` candidates during ingest;
  - existing pending false positives can be dismissed through
    `dismiss_directory_duplicate_candidates`.
- Runtime checks:
  - `python manage.py check`: ok;
  - `/health/`: ok;
  - new management command available inside the `web` container.
- Initial dry-run with `--limit=100` evaluated old GitHub/Gemini duplicate
  candidates and dismissed nothing.
- Expanded dry-run with `limit=5000`:
  - `evaluated=3710`;
  - `would_dismiss=24`;
  - all matches were directory-host-only false positives on `claude.com` or
    `chatgpt.com`;
  - high-confidence same-name candidates such as `Atlassian Rovo` remained
    pending.
- Applied duplicate repair:
  - `dismissed=24`.

Follow-up Claude autopublish dry-run:

- `evaluated=10`;
- `would_publish=5`;
- candidates:
  - `aiera`;
  - `airtable`;
  - `airwallex`;
  - `audible`;
  - `aura`.

Applied autopublish:

```bash
python manage.py autopublish_candidates \
  --source-type claude_connectors --limit 50 --apply
```

Result:

- `published=5`;
- `App.status` counts after run: `draft=15180`, `published=20`;
- Claude published count: `20`;
- `LLMCallLog`: unchanged at `1287`;
- total dismissed duplicate candidates: `24`.

Newly published apps:

- `aiera`
- `airtable`
- `airwallex`
- `audible`
- `aura`

Validation:

- All 5 public pages returned HTTP 200.
- Public API `GET /api/v1/apps/?platform=claude&page_size=30` returned
  `count=20` and includes the new apps.
- Public sitemap includes the new app URLs.

Remaining Claude blockers after this run:

- `adobe-journey-optimizer`: MCP taxonomy + short description/capability +
  launch-status review change
- `alltrails`: `short_description_lt_20`
- `alma`: `category_required`
- `amplitude`: MCP source/platform + short description
- `atlassian`: `pending_duplicate_candidate`

## Stage 5 follow-up — trusted connector category backfill

Production run on commit `ef810cd`:

- Deployed taxonomy/category backfill:
  - new category `health-wellness`;
  - Claude source mapping `Health and wellness`/`Healthcare -> health-wellness`;
  - new dry-run/apply command `backfill_trusted_connector_categories`.
- Runtime checks:
  - `python manage.py check`: ok;
  - `/health/`: ok;
  - category `health-wellness` exists in production DB;
  - new management command available inside the `web` container.
- Migration status:
  - `python manage.py migrate --noinput` reported `No migrations to apply`;
  - migration had already been applied by container startup.

Claude-only category backfill dry-run:

- `evaluated=23`;
- `would_update=2`;
- candidates:
  - `alltrails -> health-wellness`;
  - `alma -> health-wellness`.

Applied category backfill:

```bash
python manage.py backfill_trusted_connector_categories \
  --source-type claude_connectors --limit 100 --apply
```

Result:

- `updated=2`;
- `updated_categories=2`.

Follow-up Claude autopublish dry-run:

- `evaluated=5`;
- `would_publish=1`;
- candidate: `alma`.

Applied autopublish:

```bash
python manage.py autopublish_candidates \
  --source-type claude_connectors --limit 50 --apply
```

Result:

- `published=1`;
- newly published app: `alma`;
- `App.status` counts after run: `draft=15285`, `published=21`;
- Claude published count: `21`;
- `LLMCallLog`: unchanged at `1287`.

Validation:

- Public page `https://llmappmarket.com/apps/alma/` returned HTTP 200.
- Public API `GET /api/v1/apps/?platform=claude&page_size=40` returned
  `count=21` and includes `alma`.
- Public sitemap includes:
  - `https://llmappmarket.com/apps/alma/`;
  - `https://llmappmarket.com/apps/health-wellness/`.

Notes:

- The draft count is higher than the previous checkpoint because production
  now has `mcp_registry=14267` sources. This run did not invoke LLM; the
  LLM call count remained `1287`.

Remaining Claude blockers after this run:

- `adobe-journey-optimizer`: MCP taxonomy + short description/capability +
  launch-status review change
- `alltrails`: `short_description_lt_20`
- `amplitude`: MCP source/platform + short description
- `atlassian`: `pending_duplicate_candidate`

## Stage 5 follow-up — trusted connector description backfill

Production run on commit `f770af4`:

- Deployed trusted connector description backfill:
  - new dry-run/apply command `backfill_trusted_connector_descriptions`;
  - description repair is limited to trusted non-MCP cloud connectors by
    default;
  - current short descriptions are only replaced when they are below the
    minimum usable length and a better official source description is
    available.
- Runtime checks:
  - `python manage.py check`: ok;
  - `/health/`: ok;
  - new management command available inside the `web` container.

Claude-only description backfill dry-run:

- `evaluated=23`;
- `would_update=1`;
- candidate:
  - `alltrails -> Find your next outdoor adventure with AllTrails, directly in Claude.`

Applied description backfill:

```bash
python manage.py backfill_trusted_connector_descriptions \
  --source-type claude_connectors --limit 100 --apply
```

Result:

- `updated=1`;
- updated app: `alltrails`.

Follow-up Claude autopublish dry-run:

- `evaluated=4`;
- `would_publish=1`;
- candidate: `alltrails`.

Applied autopublish:

```bash
python manage.py autopublish_candidates \
  --source-type claude_connectors --limit 50 --apply
```

Result:

- `published=1`;
- newly published app: `alltrails`;
- `App.status` counts after run: `draft=15381`, `published=22`;
- Claude published count: `22`;
- `LLMCallLog`: unchanged at `1287`.

Validation:

- Public page `https://llmappmarket.com/apps/alltrails/` returned HTTP 200.
- Public API `GET /api/v1/apps/?platform=claude&page_size=50` returned
  `count=22` and includes `alltrails`.
- Public sitemap includes:
  - `https://llmappmarket.com/apps/alltrails/`;
  - `https://llmappmarket.com/apps/travel/`;
  - `https://llmappmarket.com/apps/health-wellness/`.

Remaining Claude blockers after this run:

- `adobe-journey-optimizer`: MCP taxonomy + short description/capability +
  launch-status review change
- `amplitude`: MCP source/platform + short description
- `atlassian`: `pending_duplicate_candidate`

## Stage 5 follow-up — exact cross-platform duplicate merge

Production run on commit `f02c8ca`:

- Deployed exact-name cross-platform duplicate merge:
  - new dry-run/apply command `merge_cross_platform_duplicates`;
  - default guardrails require non-MCP, two draft cards, exact normalized name
    match, no related pending duplicate candidates, and no capability conflicts;
  - source duplicate is left `hidden` instead of deleted to avoid cascade data
    loss.
- Runtime checks:
  - `python manage.py check`: ok;
  - `/health/`: ok after Gunicorn finished booting;
  - new management command available inside the `web` container.

Global duplicate dry-run:

- `evaluated=100`;
- `would_merge=0`;
- result confirmed the command is conservative on the old broad duplicate
  queue because the first 100 rows are blocked by non-exact names, MCP identity,
  related pending candidates, or capability conflicts.

Targeted Atlassian dry-run:

- duplicate candidate `3702`;
- `app=atlassian-rovo`;
- `candidate=atlassian`;
- `target=atlassian-rovo`;
- `source=atlassian`;
- `would_merge=true`;
- blockers: none.

Applied targeted duplicate merge:

```python
apply_duplicate_merge(3702, include_mcp=False)
```

Result:

- `merged=true`;
- moved `sources=2`;
- merged `platforms=1`, `categories=1`, `capabilities=13`, `use_cases=1`;
- moved `review_entries=1`, `enrichment_tasks=1`;
- duplicate candidate `3702` resolved as `confirmed`;
- source duplicate `atlassian` set to `hidden`;
- pending duplicate candidates around `atlassian` / `atlassian-rovo`: `0`.

Post-merge data repair:

- Claude platform link on `atlassian-rovo` was corrected to
  `https://claude.com/connectors/atlassian`.
- Empty ChatGPT/Claude platform `scope_summary` values were filled with the
  merged cross-platform summary.
- Review entry `195` was accepted.
- Review entry `9` was resolved as `no_action` because it was superseded by
  the merged scope summary.

Follow-up Claude autopublish dry-run:

- `evaluated=3`;
- `would_publish=1`;
- candidate: `atlassian-rovo`.

Applied autopublish:

```bash
python manage.py autopublish_candidates \
  --source-type claude_connectors --limit 50 --apply
```

Result:

- `published=1`;
- newly published app: `atlassian-rovo`;
- `App.status` counts after run: `draft=15894`, `hidden=1`, `published=23`;
- Claude published count: `23`;
- `LLMCallLog`: unchanged at `1287`.

Validation:

- Public page `https://llmappmarket.com/apps/atlassian-rovo/` returned HTTP
  200.
- Public API `GET /api/v1/apps/?platform=claude&page_size=60` returned
  `count=23` and includes `atlassian-rovo`.
- Public API shows `atlassian-rovo` on both `chatgpt` and `claude`.
- Public sitemap includes:
  - `https://llmappmarket.com/apps/atlassian-rovo/`.

Remaining Claude blockers after this run:

- `adobe-journey-optimizer`: MCP taxonomy + short description/capability +
  launch-status review change
- `amplitude`: MCP source/platform + short description
