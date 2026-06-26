# Scheduled tasks inventory

Дата: 2026-06-26.

Источник: `config/settings/base.py::CELERY_BEAT_SCHEDULE` и реализации task
functions. Production `django_celery_beat` нужно сверять отдельно, потому что
DB-schedule может отличаться от кода после ручных admin-правок.

## Production сверка без секретов

```bash
docker compose exec web python manage.py shell -c "
from django.conf import settings
from django_celery_beat.models import PeriodicTask
print('AGENT_SOURCES_ENABLED=', ','.join(settings.AGENT_SOURCES_ENABLED))
print('AGENT_ENRICH_PENDING_SOURCE_TYPES=', ','.join(settings.AGENT_ENRICH_PENDING_SOURCE_TYPES))
print('AGENT_REACTUALIZATION_ENABLED=', settings.AGENT_REACTUALIZATION_ENABLED)
for t in PeriodicTask.objects.order_by('name'):
    print(t.name, t.enabled, t.task, t.crontab_id, t.interval_id)
"
```

Если MCP enrichment отложен, `AGENT_ENRICH_PENDING_SOURCE_TYPES` не должен
содержать `mcp_registry` или `all`.

## Таблица задач

| Task | Schedule UTC | Group | Flag/gate | DB writes | LLM | Safe check | Validation / rollback |
|---|---:|---|---|---|---|---|---|
| `apps.sources.tasks.ingest_mcp_registry` | daily 04:00 | direct ingest | none | `App`, `Source`, taxonomy links, `UnparsedRegistryRecord` via upsert | no | no dry-run/limit in the management command; manual applied run supports only `--mcp-start-cursor` and `--mcp-timeout` | Check source/app counts, duplicate candidates, `UnparsedRegistryRecord`; rollback by restoring DB backup or removing newly created source/app rows from the run window |
| `apps.agent.tasks.ingest_gemini_extensions` | daily 04:30 | direct ingest | `AGENT_SOURCES_ENABLED=gemini_extensions` | `AgentRun`, `App`, `Source`, taxonomy links | no | `python manage.py agent_run --source=gemini_extensions --limit=10` then `--apply` for limited run | `AgentRun.stats` seen/new/updated/skipped/failed; repeat run should not create duplicates |
| `apps.agent.tasks.ingest_claude_connectors` | Tue 04:45 | direct ingest | `AGENT_SOURCES_ENABLED=claude_connectors` | `AgentRun`, `App`, `Source`, taxonomy links | no | `python manage.py agent_run --source=claude_connectors --limit=10` then `--apply` | Same counters; inspect `Source.source_type=claude_connectors` |
| `apps.agent.tasks.ingest_chatgpt_apps` | Wed 04:45 | direct ingest | `AGENT_SOURCES_ENABLED=chatgpt_apps` | `AgentRun`, `App`, `Source`, taxonomy links | no | `python manage.py agent_run --source=chatgpt_apps --limit=10` then `--apply` | Same counters; inspect `Source.source_type=chatgpt_unofficial` |
| `apps.sources.tasks.check_app_links_batch` | daily 05:00 | catalog maintenance | none | `LinkCheckResult`, `LinkHealth`, may set `App.launch_status=deprecated`, may queue `vanished` review after threshold | no | run with small `batch_size=5` in shell | Check checked/failed counts, new `LinkHealth`; rollback false deprecations in admin and resolve `vanished` entries |
| `apps.search.tasks.refresh_search_vectors_batch` | daily 03:00 | catalog maintenance | none | `App.search_index_text`, `App.search_vector` | no | shell call with `batch_size=10` | Search smoke: `/apps/?q=...`; rollback unnecessary, deterministic rebuild |
| `apps.search.tasks.update_popular_searches` | daily 03:30 | catalog maintenance | none | `PopularSearch` upsert | no | direct shell call | Check `PopularSearch` counts; rollback by deleting generated rows if needed |
| `apps.seo.tasks.rebuild_sitemap` | every 30 min | infrastructure | none | cache invalidation only | no | direct shell call | `curl /sitemap.xml`; rollback unnecessary |
| `apps.seo.tasks.ping_search_engines` | daily 08:00 | infrastructure | none | none | no | manual call only after sitemap is healthy | External ping result dict; no rollback |
| `apps.seo.tasks.generate_seo_reports` | Mon 08:15 | infrastructure | none | none, log-only report | no | direct shell call | Check returned report/logs |
| `apps.analytics.tasks.calculate_trending_scores` | daily 02:30 | catalog maintenance | none | `TrendingScore` upsert | no | direct shell call | Check updated/error counts; rollback by recalculating or deleting `TrendingScore` rows |
| `apps.analytics.tasks.cleanup_old_analytics_data` | Sun 04:30 | retention | none | deletes old `ClickEvent`, `PageView` | no | no dry-run implemented; inspect counts first with matching cutoff query | Restore from backup if over-deleted |
| `apps.catalog.tasks.recalc_quality_scores_batch` | daily 06:00 | catalog maintenance | none | `App.quality_score` | no | direct call with `batch_size=20` | Check processed/changed; rollback by recalculating after fixing inputs |
| `apps.newsletter.tasks.create_weekly_draft` | Fri 06:00 | infrastructure/editorial | existing weekly draft prevents duplicate | `Issue`, `IssueApp` | no | direct call in staging or inspect existing draft first | Delete draft issue if unwanted |
| `apps.agent.tasks.send_review_queue_digest` | daily 07:30 | infrastructure | recipients configured | email send only | no | call when queue/SMTP ready; with no recipients it returns skipped | Check sent count/logs; no DB rollback |
| `apps.agent.tasks.discover_rss` | every 6h | discovery | `AGENT_SOURCES_ENABLED=rss`, budget 80% gate | `AgentRun`, `EnrichmentTask`, `LLMCallLog`, and for relevant applied items `App`/`Source`/review queue | yes, cheap classification + primary enrichment | `python manage.py agent_run --source=rss --limit=5` dry-run first | Check cost in `LLMCallLog`, `AgentRun.stats`; rollback new drafts/review entries from run |
| `apps.agent.tasks.discover_github_mcp` | Mon/Wed/Fri 06:30 | discovery | `AGENT_SOURCES_ENABLED=github_mcp`, budget 80% gate | same as RSS | yes | `python manage.py agent_run --source=github_mcp --limit=5` dry-run first | Same as RSS |
| `apps.agent.tasks.enrich_pending_drafts_batch` | daily 06:45 | enrichment | `AGENT_SOURCES_ENABLED=enrich_pending`, budget 80% gate, `AGENT_ENRICH_PENDING_SOURCE_TYPES` | `AgentRun`, `EnrichmentTask`, `LLMCallLog`, `Source(agent_enrich)`, draft field merges, review queue | yes, primary | shell: `enrich_pending_drafts_batch(limit=3, dry_run=True)`; applied only after checking allowlist | Check `source_types` in returned stats; MCP requires explicit `mcp_registry`/`all`; rollback by reverting field changes from audit or DB backup |
| `apps.agent.tasks.reactualize_apps_batch` | daily 07:00 | re-actualization | `AGENT_REACTUALIZATION_ENABLED=True`, budget hard-stop | dry-run audit rows; applied writes `NeedsReviewQueueEntry(kind=reactualized)`, `Source.last_enriched_at`, `Source.payload` | yes, primary | shell: `reactualize_apps_batch(limit=3, dry_run=True)` | Confirm no App field writes, only review queue/source metadata; resolve/delete bad queue entries |
| `apps.agent.tasks.agent_budget_check` | hourly :15 | infrastructure | `AGENT_MONTHLY_BUDGET_USD`; no enforcement if unset/0 | `BudgetMonthState`, email on threshold | no | direct shell call; avoid fake low budget unless planned | Check budget row/latches; rollback by admin-clearing latches after operator review |
| `apps.agent.tasks.cleanup_old_agent_logs` | Sun 04:00 | retention | none | deletes old `AgentRun` cascade, resolved review queue | no | no dry-run implemented; inspect cutoff counts first | Restore from backup if over-deleted |
| `apps.sources.tasks.cleanup_old_link_check_results` | Sun 04:15 | retention | none | deletes old `LinkCheckResult`; keeps `LinkHealth` | no | no dry-run implemented; inspect cutoff counts first | Restore from backup if over-deleted |
| `apps.search.tasks.cleanup_old_search_logs` | Sun 04:45 | retention | none | deletes old `SearchLog` | no | no dry-run implemented; inspect cutoff counts first | Restore from backup if over-deleted |

## Manual-only до отдельного решения

- Full MCP enrichment: only with explicit budget approval and
  `AGENT_ENRICH_PENDING_SOURCE_TYPES=all` or `mcp_registry`.
- Discovery applied runs (`rss`, `github_mcp`): keep disabled until dry-run
  cost/quality is reviewed.
- Retention cleanups: safe operationally, but no dry-run mode yet; do count
  queries before first production execution.
- Link checker auto-deprecates after repeated failures. Run limited first and
  inspect `vanished` review entries.
