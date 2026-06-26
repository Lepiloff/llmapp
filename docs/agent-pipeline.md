# План: полу-автоматический LLM-pipeline наполнения каталога

## Context

У проекта есть готовый backend (Django + Postgres + Redis + Celery) и доменная модель (`App`, `Source`, `AppPlatform`, `AppDraft`). Существуют две версии MCP-ingest:

- **Целевая** (`apps/sources/mcp_registry.py` + `apps/sources/upsert.py`) — `MCPRegistrySource` нормализует записи в `AppDraft`, `upsert_app_from_draft` создаёт `App` + `AppPlatform` + `Source` с правильным `platform_verification_status=OFFICIAL`.
- **Текущая в проде** (`apps/sources/tasks.py:24-225`) — Celery-задача дублирует логику inline, **обходит** `MCPRegistrySource` и `upsert_app_from_draft`, **не создаёт** `AppPlatform`, ставит `NOT_LISTED` вместо `OFFICIAL`.

Это half-finished refactoring. Любой LLM-pipeline поверх этого унаследует расхождение. Поэтому **Phase 0 — починка существующего ingest** идёт ДО любых новых функций.

**Цель плана:** построить полу-автономный pipeline, который запускается по расписанию и закрывает 5 задач:

1. **Discovery** — находит кандидатов из внешних источников.
2. **Fetch** — забирает данные источника с уважением robots.txt и rate-limit.
3. **Enrichment** — LLM преобразует сырые данные в структурированный `AppDraft` / merge-set для существующей карточки.
4. **Validation & Persistence** — проверяет (URL liveness, дедуп, hallucination guardrails) и пишет в БД как DRAFT.
5. **Re-actualization** — периодически перепроверяет существующие карточки, диффит, **все App-field изменения идут в очередь редактора**.

**Hard constraint** (business.md): LLM **никогда** не публикует автоматически и **никогда** не перезаписывает редакторские правки. Все его выводы идут в DRAFT, `editorial_review_status = UNREVIEWED`, финальное решение всегда за редактором.

**Цель архитектуры:** код, который потенциально вынесется в отдельный автономный сервис, изолирован от Django с самого начала (через `TaxonomySnapshot`-seam) — извлечение позже это перемещение файлов, а не переписывание.

---

## Архитектурное решение: где живёт агент

Создать новое Django-приложение `apps/agent/` со строгой внутренней изоляцией:

```
apps/agent/
├── llm/                    # pure Python; нет импортов Django
│   ├── client.py           # LLMProvider abstraction (Anthropic, OpenAI — pluggable, ни один не mandatory)
│   ├── prompts.py          # versioned prompts (v1.0, v1.1, ...)
│   └── schemas.py          # Pydantic для structured output
├── pipeline/               # pure Python; принимает TaxonomySnapshot, не импортирует Django
│   ├── taxonomy.py         # TaxonomySnapshot dataclass — единственный способ передать БД-таксономию в pipeline
│   ├── fetch.py            # httpx async, robots, ETag; вызывает rate_limit.get_default_limiter()
│   ├── rate_limit.py       # NoopRateLimiter + InMemoryDomainRateLimiter + set/get_default_limiter() registration point
│   ├── enrich.py           # url+rawdata + TaxonomySnapshot → EnrichedDraft via LLM
│   ├── merge.py            # merge policy для enrich_existing_draft (см. Phase 1)
│   ├── validate.py         # URL liveness, dedup helpers, guardrails
│   └── reactualize.py      # diff existing App vs fresh fetch
├── sources/                # discovery sources (новые BaseSource impls)
│   ├── rss_feeds.py
│   ├── github_mcp_search.py
│   ├── chatgpt_apps.py
│   ├── claude_connectors.py
│   └── gemini_extensions.py
├── persist.py              # ЕДИНСТВЕННАЯ точка контакта с apps.catalog
│                           # Содержит build_taxonomy_snapshot(), persist_new_draft(), apply_merge_set()
├── rate_limit_redis.py     # Django layer: RedisDomainRateLimiter + build_limiter_from_settings();
│                           # импортирует django_redis, поэтому ВНЕ pipeline/
├── models.py               # Django: AgentRun, EnrichmentTask, LLMCallLog, NeedsReviewQueueEntry
├── tasks.py                # тонкие Celery wrappers — готовят TaxonomySnapshot, вызывают pipeline, пишут результат
├── admin.py                # observability + review queue UI
├── apps.py                 # AgentConfig.ready() регистрирует limiter в pipeline.rate_limit
└── management/commands/
    ├── agent_run.py        # ручной one-off запуск
    └── agent_reactualize.py
```

**Почему так:**

- `llm/`, `pipeline/`, `sources/` — pure Python, никаких `from django.* import`. Будущий микросервис мигрирует as-is.
- `pipeline/taxonomy.py::TaxonomySnapshot` — frozen dataclass с допустимыми slug-ами Platform/Category/Capability/ListingType. Готовится в `persist.py::build_taxonomy_snapshot()` (читает БД) и передаётся в pipeline как параметр. Это закрывает протечку Django в "pure" слой.
- `persist.py` — единственный мост к ORM. При вынесении заменяется на HTTP-клиент к `POST /internal/agent/upsert-draft` без касания pipeline-кода.
- Celery beat и Django Admin дают observability и ручной контроль бесплатно.
- Lambda для текущей нагрузки не подходит (enrichment 50–200 кандидатов × 20–40 сек ≫ 15-мин лимита; потребует chunking + Step Functions — больше сложности, чем уже работающий Celery).

---

## Phasing

### Phase 0 — Стабилизация существующего ingest

**Цель:** привести текущий MCP Registry ingest в соответствие с целевой архитектурой ДО построения LLM-pipeline. Без этого все новые карточки будут унаследовать расхождение, а тесты обнаружат drift между ожидаемой и фактической формой данных.

**Работы:**

1. **Переписать `apps/sources/tasks.py::ingest_mcp_registry`** — заменить inline-логику (`_process_mcp_server`, `_create_app_from_mcp_server`, `_update_app_from_mcp_server`) на вызов `MCPRegistrySource().iter_drafts()` + `upsert_app_from_draft()`. Удалить дублирующие функции.
2. **Сохранить обработку `unparsed`**: после iter_drafts() записать `source.unparsed` в `UnparsedRegistryRecord` и наблюдаемые `schema_versions` в лог/Sentry-tag.
3. **Data migration** (отдельная Django migration или idempotent management command):
   - Для всех `App`, у которых есть `Source.source_type=MCP_REGISTRY` и нет `AppPlatform` записи с `platform__slug='mcp'` → создать `AppPlatform` через `attach_platforms()` (использовать `payload` из `Source` для protocol_version/transport/repository_url).
   - Для тех же `App`: если `platform_verification_status == NOT_LISTED` → переключить на `OFFICIAL` (MCP Registry — это и есть официальный directory согласно business.md § 6.5).
4. **Pre-check для остальных claim-ов из feedback** (read-only, документировать факты):
   - `apps/search/` (`refresh_search_vector`): signal-based refresh работает корректно
     (`apps/catalog/signals.py` + `apps/search/tasks.py`). **Sprint 1 (п.2)** дополнительно
     исправил, что vector индексировал только 4 базовых колонки (`name`, `short_description`,
     `developer_name`, `long_description`) — теперь включает агрегированный `search_index_text`
     с platform/category/use-case names, чтобы FTS-запросы вида "mcp servers" находили
     карточки по таксономии, а не только по trigram-fallback'у. Регрессия:
     `tests/search/test_fts_taxonomy_match.py`.
   - URL-routes (`config/urls.py` + `apps/*/urls.py`): verified — sitemap, health,
     catalog/search/admin/submit/blog/newsletter/go/<platform>/<category> зарегистрированы.
   - `TrigramExtension` + `pg_trgm` GIN-индексы: verified — миграции `apps/catalog/migrations`
     создают `pg_trgm` extension и GIN-индексы на `name` / `developer_name`
     (`app_name_trgm_gin`, `app_dev_trgm_gin`). `/health/` пробит `similarity('a'::text, 'a'::text)`.

   Если что-то из этого реально сломано — фикс расширяет Phase 0; если работает — фиксируем "verified" в комментарии и идём дальше.

5. **Regression-тесты** для починенного ingest: фикстурный JSON registry → одна транзакция → ожидаемые `App` + `AppPlatform` + `Source` рядом.

**Выход:** ingest MCP Registry создаёт корректные `App` + `AppPlatform` + `Source` рядом через единый путь. Существующие данные согласованы.

**Никаких LLM-зависимостей здесь ещё нет.** Phase 0 — это рефакторинг, который должен пройти даже если LLM-pipeline никогда не запустится.

**Phase 0 → Phase 1 acceptance criteria** (quality gate, не календарный):

Переход к Phase 1 разрешён только когда **все** пункты выполнены и зафиксированы в PR/commit notes или в актуальном checklist-документе:

1. *(quantitative)* 100% MCP-импортированных `App` имеют запись `AppPlatform` для платформы `mcp` (`AppPlatform.objects.filter(platform__slug='mcp', app__sources__source_type='mcp_registry').distinct().count() == App.objects.filter(sources__source_type='mcp_registry').distinct().count()`).
2. *(quantitative)* 100% MCP-импортированных `App` имеют `platform_verification_status='official'` (a не `not_listed`/`unknown`).
3. *(quantitative)* `ingest_mcp_registry` идемпотентен: второй запуск подряд с одинаковым registry payload даёт 0 новых `App`, 0 изменённых `AppPlatform`, 0 новых `Source`.
4. *(quantitative)* `apps/sources/tasks.py` больше не содержит функций `_process_mcp_server`, `_create_app_from_mcp_server`, `_update_app_from_mcp_server` (grep подтверждает удаление).
5. *(qualitative)* Schema-mismatched registry-записи попадают в `UnparsedRegistryRecord`, не в worker crash log. Smoke-тест на специально сломанном payload это подтверждает.
6. *(qualitative)* Pre-check состояния `apps/search/` (signal-based `refresh_search_vector`), URL-routes (`config/urls.py` + `apps/*/urls.py`), миграции `TrigramExtension`/`pg_trgm` задокументирован в PR/commit notes или исправлен отдельным PR.
7. *(operational)* Regression-тесты `tests/sources/test_ingest_mcp_registry.py` зелёные, покрывают: создание новой App, обновление существующей App, schema-mismatch row.
8. *(operational)* Все существующие тесты репозитория зелёные (`pytest`).

---

### Phase 1 — Foundation + enrichment существующих MCP-черновиков

**Цель:** доказать ценность LLM-обогащения на самом простом use case — догрузить пустые поля в существующих DRAFT-карточках MCP Registry (capabilities, categories, use_cases, long_description). Никакого discovery, никакого скрейпинга.

**Зависимости** (`pyproject.toml`):
- `anthropic>=0.40`, `openai>=1.50` (обе библиотеки — но конкретный провайдер выбирается через env)
- `pydantic>=2.5`, `httpx[http2]>=0.27`, `tenacity>=9.0`, `beautifulsoup4>=4.12`, `lxml>=5.0`, `feedparser>=6.0`

**Env vars** (без хардкода моделей в коде — всё через env, чтобы переключать провайдера/модель без релиза):
- `AGENT_LLM_PROVIDER_PRIMARY=anthropic|openai` — провайдер для основного enrichment.
- `AGENT_LLM_PROVIDER_CHEAP=anthropic|openai` — провайдер для дискавер-классификации (может быть тем же или другим).
- `AGENT_LLM_MODEL_PRIMARY=` — имя модели, без default в коде; пользователь выбирает текущую актуальную.
- `AGENT_LLM_MODEL_CHEAP=` — то же для дешёвой модели.
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` — каждый optional; обязателен только тот, чей провайдер выбран. Конфиг-валидация при старте кричит, если выбранный провайдер не имеет ключа.
- `AGENT_MONTHLY_BUDGET_USD=` — без default; обязателен.
- `AGENT_RATE_LIMIT_RPS_PER_DOMAIN=1.0` — enforced через `DomainRateLimiter` (Sprint 1). Применяется ко всем outbound fetcher'ам (RSS, GitHub Search/Contents, `fetch_url_text`).
- `AGENT_SOURCES_ENABLED=` — feature flag (csv), на старте пусто (всё выключено).

**Pure-Python core:**

- `apps/agent/llm/client.py` — абстракция `LLMProvider` (метод `complete(system, messages, schema: type[BaseModel]) -> BaseModel`). Реализации:
  - `AnthropicProvider` — tool use для гарантии валидного JSON; prompt caching для system+few-shot.
  - `OpenAIProvider` — JSON schema response_format.
  - Каждый вызов записывает `LLMCallLog` через коллбек, переданный из Django-слоя (контроллер не знает про ORM, просто вызывает callback с метриками).
  - Tenacity-retry (3 попытки, экспоненциальный backoff на 429/5xx).

- `apps/agent/pipeline/taxonomy.py`:
  ```python
  @dataclass(frozen=True)
  class TaxonomySnapshot:
      platform_slugs: tuple[str, ...]
      category_slugs: tuple[str, ...]
      capability_keys: tuple[str, ...]
      listing_type_slugs: tuple[str, ...]
      capability_descriptions: dict[str, str]
      category_descriptions: dict[str, str]
  ```

- `apps/agent/llm/schemas.py` — Pydantic-модели:
  - `EnrichedDraft` — структурированный output: поля + `evidence_map[field_name] -> quote`.
  - `MergeSet` — целевая структура для `enrich_existing_draft`: какие поля LLM **предлагает** дописать в существующую DRAFT-карточку.

- `apps/agent/llm/prompts.py` — versioned (v1.0…). Каждый prompt принимает `TaxonomySnapshot` для контекста (allowed slugs).

- `apps/agent/pipeline/enrich.py`:
  - `enrich_new_app(raw_sources: list[FetchResult], taxonomy: TaxonomySnapshot, llm: LLMProvider) -> EnrichedDraft` — для нового кандидата.
  - `enrich_existing_draft(current: AppSnapshot, raw_sources: list[FetchResult], taxonomy: TaxonomySnapshot, llm: LLMProvider) -> MergeSet` — для дообогащения. `AppSnapshot` — pure-Python dataclass с текущими значениями полей (готовится Django-слоем).

- `apps/agent/pipeline/merge.py` — **merge policy для существующих DRAFT-карточек** (отдельная от `upsert_app_from_draft`):
  - `short_description`, `long_description`, `developer_name`, `official_page_url`, `install_url`, `repo_url` — заполняются только если текущее значение пустое.
  - `AppCapability`: для каждого `cap_key` LLM возвращает `(value, evidence)`. Записываем `yes/no` **только если** текущее значение `UNKNOWN` и `evidence` непустой. Никогда не меняем existing `yes/no`.
  - `AppUseCase`: добавляем новые use_case как `get_or_create(slug)` + `attach`; existing не трогаем.
  - `AppCategory`: добавляем только если `confidence >= 0.7` и категория ещё не присвоена.
  - `App.platforms`/`listing_types`: добавляем missing, никогда не удаляем.
  - `App.launch_status`, `App.pricing_model` — **никогда не меняем автоматически**, только предложение в `Source.payload['proposed_changes']`.
  - `App.verdict` — **никогда не пишется LLM напрямую**, всегда `Source.payload['proposed_verdict']`.
  - `App.editorial_review_status`, `App.status`, `App.developer_claim_status`, `App.platform_verification_status` — **read-only для агента**.

**Django-слой:**

- `apps/agent/models.py`:
  - `AgentRun(source_type, started_at, finished_at, status, stats_json, total_cost_usd, triggered_by)`.
  - `EnrichmentTask(run FK, app FK nullable, status, source_url, error)`.
  - `LLMCallLog(task FK, provider, model, input_tokens, output_tokens, cached_tokens, cost_usd, prompt_version, latency_ms)`.
  - `NeedsReviewQueueEntry(app FK, kind: enriched|reactualized|vanished, payload: JSON diff, created_at, resolved_at, resolved_by)` — для Phase 2.

- `apps/agent/persist.py`:
  - `build_taxonomy_snapshot() -> TaxonomySnapshot` — читает Platform/Category/Capability/ListingType из БД.
  - `build_app_snapshot(app_id) -> AppSnapshot` — текущее состояние App + связанных таблиц как pure-Python dataclass.
  - `apply_merge_set(app_id, merge_set, source_payload_addendum)` — атомарно применяет MergeSet к существующей App по правилам выше. Возвращает `NeedsReviewQueueEntry` ID (т.к. даже после merge-применения LLM-предложения по launch/pricing/verdict идут в queue).
  - `persist_new_draft(enriched, source_type, raw_payload)` — обёртка над `upsert_app_from_draft` для **новых** карточек (Phase 3+).

- `apps/agent/tasks.py`:
  - `enrich_existing_draft_task(app_id)` — основной таск Phase 1.
  - `enrich_pending_drafts_batch(limit=10)` — beat, ежедневно; селектор: DRAFT-карточки + Source.payload без `enriched_at`.

**Тесты:**
- `tests/agent/llm/test_client.py` — replay-фикстуры для обоих провайдеров, проверка retry/cost/schema-validation.
- `tests/agent/pipeline/test_enrich.py` — mock LLMProvider, фикстура HTML → ожидаемая `EnrichedDraft` / `MergeSet`.
- `tests/agent/pipeline/test_merge.py` — table-driven: для каждой комбинации (current_app_state × merge_set) → ожидаемое финальное состояние, с особым вниманием к "never overwrite human edits".
- `tests/agent/test_persist.py` — hard constraints: `App.status=DRAFT`, `editorial_review_status=UNREVIEWED`, `App.verdict=""` после `apply_merge_set`.

**Phase 1 → Phase 2 acceptance criteria** (quality gate, не календарный):

Переход к Phase 2 разрешён только когда **все** пункты выполнены и зафиксированы в PR/commit notes или в актуальном checklist-документе:

1. *(quantitative)* ≥ 20 MCP DRAFT обогащены в `--dry-run` без записи в БД (логи `EnrichmentTask.status=enriched`, `app=None`).
2. *(quantitative)* ≥ 10 MCP DRAFT обогащены с реальной записью в `NeedsReviewQueueEntry`.
3. *(quantitative)* **0** изменений в `App.status`, `App.verdict`, `App.editorial_review_status`, `App.platform_verification_status`, `App.developer_claim_status` за период Phase 1 (SQL-аудит по `App.updated_at` ≥ `phase1_start` — никаких изменений в перечисленных колонках).
4. *(quantitative)* **0** published apps затронуто (`App.objects.filter(status='published', updated_at__gte=phase1_start).count() == 0`).
5. *(qualitative)* У **каждой** `AppCapability` с `value ∈ {yes, no}`, записанной агентом, есть непустой `evidence` в `Source.payload['evidence_map']`. Проверка через SQL / management command.
6. *(qualitative)* Средний cost/card и latency/card зафиксированы в `AgentRun.stats_json` за последние 10 runs. Значения попадают в Cost model (см. секцию ниже) — иначе пересмотр модели / промптов перед Phase 2.
7. *(qualitative)* Редактор вручную **принял** ≥ 6 из 10 предложений LLM (через будущий Phase 2 admin или временный `manage.py` command для review). Acceptance rate < 60% → вернуться к промптам, не идти дальше.
8. *(operational)* Все unit-тесты `tests/agent/` зелёные.
9. *(operational)* Выявленные edge-cases (где LLM регулярно ошибается, галлюцинирует) фиксируются в PR/commit notes или в актуальном чеклисте. Phase 2 admin UI должен явно сигнализировать эти паттерны редактору.

Если пункт #7 (acceptance rate) не достигнут — это не баг тестов, а сигнал, что качество промптов недостаточно для масштабирования. В этом случае — итерации над `prompts.py` (v1.0 → v1.1), regression-evals (см. Phase 5 eval pack), и повтор Phase 1 measurement, **без** перехода к Phase 2.

---

### Phase 2 — Admin review queue

**Цель:** дать редактору UI для работы с LLM-предложениями. До этого Phase 1 пишет в DB, но редактор не видит структурированно что было предложено.

- Admin-страница для `NeedsReviewQueueEntry`:
  - Слева: текущие поля App.
  - Справа: предложения LLM (с `evidence_map`, версией промпта, моделью, ID `LLMCallLog`).
  - Inline-кнопки: "Apply proposed verdict", "Apply proposed launch_status", "Apply proposed pricing", "Reject all", "Mark resolved".
  - Bulk action "approve & publish via transition_to_published" — только для карточек, прошедших чек-лист публикации (business.md § 11.2).
- Admin-фильтры: kind=enriched/reactualized/vanished, by source_type, by app.
- Email-уведомление редактору при заполнении queue (один сводный digest утром, не per-entry).
- Тесты: фикстурные `NeedsReviewQueueEntry` → smoke-тест admin views (status 200, ключевые поля видны).

---

### Phase 3 — Discovery: RSS + GitHub

**Цель:** добавить **безопасные** источники с публичным API/RSS. ToS-чистые, без скрейпинга вендорских директорий.

- `apps/agent/sources/rss_feeds.py`:
  - Feeds: блог Anthropic, OpenAI блог, Google AI blog, MCP-релевантные GitHub Atom feeds (`/topic/mcp-server/feed`).
  - Per-post: cheap-LLM-фильтр "анонс приложения/коннектора/MCP-сервера? YES/NO + URL".
  - YES → enqueue `enrich_new_app_task(url, source_type)`.

- `apps/agent/sources/github_mcp_search.py`:
  - GitHub Search API: `q=topic:mcp-server stars:>5 pushed:>now-90d`. Использует authenticated requests (env: `GITHUB_TOKEN`) для rate limit 5000/h.
  - Per repo: GitHub Contents API → README.md → LLM-парсинг в `EnrichedDraft`.
  - Дедуп против существующих `Source.external_id` (по MCP Registry id или repo URL).

- `apps/agent/tasks.py`:
  - `enrich_new_app_task(url, source_type)` — fetch + enrich_new_app + persist_new_draft.
  - `discover_rss()`, `discover_github_mcp()` — beat.

- Beat schedule:
  - `agent_discover_rss`: каждые 6 часов.
  - `agent_discover_github_mcp`: 3 раза в неделю.

- Тесты: snapshot-тесты RSS-фикстур и README-фикстур.

**Production gate Phase 3 → Phase 4:** не переходим, пока:
1. Не накоплено ≥ 20 LLM-сгенерированных DRAFT через RSS/GitHub.
2. Approval rate (доля DRAFT, дошедших до PUBLISHED) ≥ 50% — иначе шум перевешивает пользу.
3. Cost per published app измерен и зафиксирован.

---

### Phase 4 — Official directories + re-actualization

**Цель:** добавить ChatGPT Apps / Claude Connectors / Gemini Extensions + регулярную перепроверку. Самая рискованная часть — source drift, ToS-чувствительность и риск тихого затирания редакторских правок.

**Prerequisites (link-checker, deferred from Phase 0):**

Phase 4 re-actualization строится поверх существующего link-checker. До запуска любой автоматизации re-actualization нужно починить два бага в `apps/sources/tasks.py::check_app_links_batch`:

1. `App.published.filter(last_checked_at__lt=cutoff)` исключает never-checked apps (где `last_checked_at IS NULL`). Меняем на `Q(last_checked_at__lt=cutoff) | Q(last_checked_at__isnull=True)`.
2. Auto-deprecate срабатывает при `consecutive_failures >= 5`; per business.md § 11.3 и architecture.md § 11 должно быть **7**.

Оба фикса с regression-тестами обязательны до включения `agent_reactualize` в beat-schedule.

**Discovery — директории и сторонние индексы:**

- Перед началом — **юридическая проверка ToS** каждой директории. Если запрещён программный доступ — пропускаем источник или ищем официальный API.
- Реализовано для MVP:
  - `apps/agent/sources/gemini_extensions.py` — JSON ingest.
  - `apps/agent/sources/claude_connectors.py` — static HTML + BS4, robots.txt enforcement.
  - `apps/agent/sources/chatgpt_apps.py` — static HTML + BS4 по стороннему crawlable index.
  - Conservative scraping: 1 RPS на домен, User-Agent identifying, robots.txt для HTML-crawl источников.
  - Failure mode: ошибка скрейпинга → Sentry/log counters, batch не публикует автоматически; следующий run попробует снова.
- ChatGPT MVP использует `mcpapp.net/chatgpt-apps` как third-party
  crawlable index и пишет `Source.source_type=chatgpt_unofficial`;
  official OpenAI source остаётся отдельным hardening item.
- Beat: direct-ingest источники gated через `AGENT_SOURCES_ENABLED`.

**Production update cadence:**

- Первичное наполнение каталога - разовый bootstrap. Регулярные задачи не
  должны повторять тот же объём LLM-работы.
- Direct-ingest источники (`mcp_registry`, `gemini_extensions`,
  `claude_connectors`, `chatgpt_apps`) можно запускать по расписанию:
  они заново читают внешний источник, нормализуют записи в `AppDraft` и
  пишут через `upsert_app_from_draft`. Дедупликация идёт по
  `Source(source_type, external_id)`: известные записи обновляются, новые
  добавляются, карточки не публикуются автоматически. LLM на этом этапе не
  вызывается.
- `enrich_pending_drafts_batch` обрабатывает только DRAFT-карточки, у
  которых ещё нет `Source.external_id = agent-enrich:<app_id>`. Уже
  enriched карточки не прогоняются повторно; если очередной direct ingest
  нашёл 20 новых приложений, LLM должен обработать только эти 20.
- Общий `enrich_pending` selector включает MCP Registry, Gemini, Claude и
  ChatGPT. После завершения non-MCP enrichment не оставлять
  `enrich_pending` включённым без отдельного MCP-бюджета или source-scoped
  batch, иначе следующий beat начнёт расходовать бюджет на MCP Registry.
- MCP Registry direct ingest разрешён как регулярная сверка без LLM.
  Полный MCP enrichment - отдельный операторский/бюджетный запуск, а не
  часть обычной daily актуализации.
- Discovery источники (`rss`, `github_mcp`) работают малыми batches,
  дедуплицируют кандидатов по `external_id` и тратят LLM только на новые
  URL, которые ещё не представлены в `Source`.
- Re-actualization работает отдельно от enrichment: выбирает только
  published apps с overdue `Source.last_enriched_at`, refetch-ит источник,
  считает diff и пишет `NeedsReviewQueueEntry(kind=reactualized)`. Она не
  повторяет initial population и не делает silent update App-полей.

**Re-actualization:**

- `Source.last_enriched_at` уже есть в модели; re-actualization использует его как cadence marker. Backfill: `last_enriched_at = fetched_at` для существующих.
- `apps/agent/pipeline/reactualize.py`:
  - Вход: `AppSnapshot` + список fresh `FetchResult`.
  - Re-run enrichment, считаем `EnrichedDraft`.
  - Diff против `AppSnapshot`.
  - **Все App-field изменения** → `NeedsReviewQueueEntry(kind=reactualized, payload=diff)`. Никаких silent auto-update в App, AppCapability, AppCategory, AppUseCase.
  - **Auto-update только metadata:** `Source.last_enriched_at`, `Source.payload`, `LinkHealth` записи (это уже metadata/audit, не редакторская правка).
  - "Vanished" detection: используем существующий `LinkHealth.consecutive_failures` паттерн. При 3 подряд 404 → `Source.is_active=False` + `NeedsReviewQueueEntry(kind=vanished)`.
- Celery: `reactualize_apps_batch(limit=20)` — beat ежедневно 07:00 UTC.
- Конфиг: `AGENT_REACTUALIZATION_INTERVAL_DAYS=30`.

**Что НЕ делаем в Phase 4:**
- Auto-deprecate карточки на основе LLM-вывода (только через существующий link-checker, который уже на месте).
- Auto-change `launch_status`, `pricing_model`, `developer_name`, `verdict` — всё через queue.
- Auto-удаление use_cases или capabilities=yes — даже если LLM считает их устаревшими, queue.

---

### Phase 5 — Observability, guardrails, безопасность (cross-cutting, начинать с Phase 1)

**Budget tracking:**
- Beat task `agent_budget_check` (ежечасно): агрегирует `LLMCallLog.cost_usd` за текущий месяц.
- При 80% от `AGENT_MONTHLY_BUDGET_USD` → email-alert + auto-disable discovery (re-actualization продолжает как более ценная).
- При 100% → hard stop: pre-task hook в `apps/agent/tasks.py` отказывает.

**Admin dashboard:** `AgentRun`, `EnrichmentTask`, `LLMCallLog` с фильтрами и агрегатами по cost.

**LLM evaluation pack** (`tests/agent/eval/`):
- 10–20 заранее размеченных образцов: (raw payload) → ожидаемый `EnrichedDraft`.
- `pytest tests/agent/eval/ --eval` → precision/recall/F1 по полям.
- Regression gate при изменении промптов: если accuracy падает >5pp — fail.

**Безопасность:**
- API keys только из env. Никогда в коде / БД / логах.
- LLM-вывод никогда не интерпретируется как код/SQL/URL без явной валидации.
- robots.txt — обязателен для HTML-crawl источников (`claude_connectors`, `chatgpt_apps`); RSS / GitHub API / Gemini JSON пути его не используют.
- User-Agent identifying (`LLMAppMarket-Agent/1.0; +https://llmappmarket.com/bots`).
- Rate limit per domain hard-enforced cross-process — `RedisDomainRateLimiter` (Sprint 1, май 2026); Lua-script atomicity coordinates все Celery worker child-processes. In-memory limiter (`apps/agent/pipeline/rate_limit.py::InMemoryDomainRateLimiter`) — fallback на старте если Redis недоступен.

**Retention** (Sprint 1, май 2026 — закрывает риск unbounded-роста audit-таблиц):
- `apps.agent.tasks.cleanup_old_agent_logs` (180 дней) — `AgentRun` + каскадно `EnrichmentTask` (FK CASCADE) + `LLMCallLog` (FK CASCADE). Pending `NeedsReviewQueueEntry` **всегда** сохраняются (даже если старше cutoff'а) — editor work не теряется.
- `apps.sources.tasks.cleanup_old_link_check_results` (30 дней) — `LinkCheckResult` audit. `LinkHealth` это rolling-summary, не трогается.
- `apps.search.tasks.cleanup_old_search_logs` (90 дней) — `SearchLog`. `PopularSearch` пересобирается из последних 30 дней `SearchLog` через `update_popular_searches`, так что 90-дневное окно безопасно.
- `apps.analytics.tasks.cleanup_old_analytics_data` (90 дней) — `ClickEvent`, `PageView`.

Параметры env: `AGENT_LOG_RETENTION_DAYS`, `SOURCES_LINK_CHECK_RETENTION_DAYS`, `SEARCH_LOG_RETENTION_DAYS`. Все задачи в beat (sunday 04:00-04:45 UTC, после link-checker и до newsletter).

---

## Cost model (эмпирическая оценка для калибровки `AGENT_MONTHLY_BUDGET_USD`)

Точные числа зависят от выбранных моделей; ниже — порядок величины для типичного MVP-сценария.

Допущения:
- Типичный enrichment: ~5K input tokens (HTML + prompt + few-shot examples) + ~2K output tokens (structured EnrichedDraft).
- ~70% input-tokens — кэшированный системный промпт + few-shots (prompt caching, поддерживается Anthropic).
- Cheap-модель для discovery-классификации: ~1K input + ~200 output, доля от стоимости primary < 5%.

Грубый расчёт **при цене порядка $3 in / $15 out за 1M tokens** (типично для top-tier Sonnet-класс модели):
- Per enrichment без caching: ~$0.045.
- Per enrichment с 70% caching: ~$0.038.
- Phase 1 (~50 существующих MCP-черновиков обогатить): ~$2.
- Phase 3 (~50 новых кандидатов/месяц через RSS+GitHub): ~$2/мес.
- Phase 4 (~300 апп × 1 re-actualization/мес): ~$12/мес.
- Discovery overhead (RSS/GitHub классификация cheap-моделью): ~$1/мес.
- **Итого baseline:** ~$15-20/мес при 300 карточках.

**Калибровка:**
- Phase 1: запустить на 5 апп, замерить реальный cost, экстраполировать.
- Установить `AGENT_MONTHLY_BUDGET_USD` = 3× ожидаемого baseline (буфер на ошибки и retry-loops).
- При ценах, отличающихся от допущений, на порядок — пересчитать перед Phase 4.

---

## Critical files

**Reuse без изменений:**
- `apps/sources/base.py:13-55` — `AppDraft` dataclass и `BaseSource` interface.
- `apps/sources/mcp_registry.py` — `MCPRegistrySource` готов, нужно лишь подключить в Phase 0.
- `apps/sources/upsert.py:57-76` — `find_soft_duplicate` для дедупа в `pipeline/validate.py`.
- `apps/sources/upsert.py:144-239` — `upsert_app_from_draft` для **новых** карточек (Phase 3+). Для существующих DRAFT используется `apps/agent/pipeline/merge.py`.
- `apps/catalog/services.py` — `recalc_quality_score`, `transition_to_published`. Агентом **не вызываются** — это редакторский путь.

**Изменения в Phase 0:**
- `apps/sources/tasks.py:24-225` — переписать `ingest_mcp_registry` на `MCPRegistrySource` + `upsert_app_from_draft`. Удалить `_process_mcp_server`, `_create_app_from_mcp_server`, `_update_app_from_mcp_server`.
- `apps/sources/migrations/000X_backfill_mcp_appplatform.py` — data migration для существующих MCP-импортированных карточек: создать `AppPlatform` + переключить `platform_verification_status` на OFFICIAL.
- `apps/sources/utils.py` — вынести `_check_app_links` как pure helper для переиспользования в `apps/agent/pipeline/validate.py`.

**Изменения в Phase 1+:**
- `apps/sources/models.py` — `Source.last_enriched_at` уже добавлен; Phase 4 использует поле для cadence/re-actualization.
- `config/settings/base.py:214-235` — расширить `CELERY_BEAT_SCHEDULE` на agent-задачи (по фазам). После Sprint 1 в расписании также operational-задачи (sitemap, retention, trending recalc, quality recalc, SEO reports, popular searches) — не только agent.
- `config/settings/base.py` — env vars для LLM, budget, rate limits. Sprint 1 добавил retention env'ы: `AGENT_LOG_RETENTION_DAYS=180`, `SOURCES_LINK_CHECK_RETENTION_DAYS=30`, `SEARCH_LOG_RETENTION_DAYS=90`.
- `pyproject.toml:6-33` — новые зависимости (Phase 1).
- `.env.example`, `.env` — новые ключи (без значений).

**Изменения в Sprint 1 (май 2026, prod-readiness):**
- `apps/sources/upsert.py::attach_platforms` — переписана под «editor wins on merge» контракт; `AppPlatform.metadata` shallow-merge'ится, остальные поля заполняются только если пустые/UNKNOWN. Закрывает Hard constraint #2 на повторных discovery-циклах. Регрессии: `tests/sources/test_attach_platforms.py`.
- `apps/search/tasks.py::refresh_app_search_vector` — индексирует platform/category/use-case names через агрегированный `search_index_text`. Регрессии: `tests/search/test_fts_taxonomy_match.py`.
- `apps/seo/tasks.py::rebuild_sitemap` — теперь реально инвалидирует кеш через `delete_pattern` + fallback `cache.clear()`. Sitemap view обёрнут в `cache_page(30min, key_prefix='sitemap_v1')`. Регрессии: `tests/seo/test_sitemap_cache.py`.
- `apps/core/healthcheck.py::_check_celery_worker` — `/health/` 503 если worker не отвечает на ping (timeout 1.5s).
- `config/settings/prod.py` — `SECRET_KEY` hard-fail без env-значения; запрещён insecure dev-default.

**Новые** (создаются в `apps/agent/`):
- См. структуру в разделе "Архитектурное решение".

**Новые модули Sprint 1:**
- `apps/agent/pipeline/rate_limit.py` — `NoopRateLimiter`, `InMemoryDomainRateLimiter`, registration-point `get/set_default_limiter()`. Pure Python — нет импортов Django. Регрессии: `tests/agent/test_rate_limit.py`.
- `apps/agent/rate_limit_redis.py` — `RedisDomainRateLimiter` (Lua-script cross-process throttle) + `build_limiter_from_settings()` factory. Django layer, импортирует `django_redis`. Регрессии: `tests/agent/test_rate_limit_redis.py`.
- `apps/seo/signals.py` — `post_save(App)` и `post_delete(App)` → `transaction.on_commit(invalidate_sitemap_cache)`. Любая модификация (включая unpublish и delete) сбрасывает кэш sitemap.
- `apps/catalog/tasks.py` — `recalc_quality_scores_batch` (daily beat).

---

## Hard constraints

1. **LLM никогда не публикует**: `App.status=DRAFT` всегда, `editorial_review_status=UNREVIEWED` всегда. Тест в `tests/agent/test_persist.py`.
2. **LLM никогда не перезаписывает редакторские правки**: для существующих `App` **и `AppPlatform`** — заполняем только пустые/UNKNOWN поля; `AppPlatform.metadata` shallow-merge'ится с editor-edits побеждающими на конфликтах; для launch_status/pricing/verdict — только предложение в queue, никогда apply. Enforce: `apps/agent/persist.py::apply_merge_set` + `apps/sources/upsert.py::attach_platforms` (последнее с Sprint 1). Регрессии: `tests/agent/test_persist.py`, `tests/sources/test_attach_platforms.py`.
3. **Verdict — поле редактора**: LLM пишет в `Source.payload['proposed_verdict']`, никогда в `App.verdict`.
4. **Capability UNKNOWN-by-default**: `yes/no` только при непустом `evidence`. Guardrail enforced в `pipeline/validate.py`.
5. **Дедупликация перед записью**: strong identity matches через `find_soft_duplicate` создают второй `Source`, а не новый `App`; weak matches создают `DuplicateCandidate` для админа, без silent merge.
6. **Re-actualization не делает silent auto-update App-полей**: все diffs → review queue.
7. **Audit trail**: каждый LLM-вызов в `LLMCallLog`, источник enrichment в `Source.payload`.
8a. **Rate limiting (per-domain) hard-enforced cross-process** через Redis-backed `RedisDomainRateLimiter` (`apps/agent/rate_limit_redis.py`, Sprint 1 follow-up). Atomic Lua-script держит "next allowed timestamp" на host в Redis, так что `worker --concurrency=2` (или несколько worker-контейнеров) соблюдают единый `AGENT_RATE_LIMIT_RPS_PER_DOMAIN` end-to-end. Pure-Python pipeline-слой (`apps/agent/pipeline/rate_limit.py`) экспонирует `get_default_limiter()` + `set_default_limiter()`; Django bridge (`AgentConfig.ready`) регистрирует Redis-implementation при старте, с soft-fallback на in-memory limiter если Redis недоступен (логируется warning). Подключён к `fetch_url_text`, GitHub Search/Contents API, RSS-fetcher. Дефолт `AGENT_RATE_LIMIT_RPS_PER_DOMAIN=1.0`. Регрессии: `tests/agent/test_rate_limit.py`, `tests/agent/test_rate_limit_redis.py`.
8b. **Robots.txt enforcement**: HTML-crawl источники (`claude_connectors`, `chatgpt_apps`) проверяют robots.txt до обхода. RSS/GitHub API/Gemini JSON на robots.txt не опираются, потому что используют публичные feeds/API endpoints.
9. **Budget cap**: hard stop при превышении месячного бюджета.
10. **TaxonomySnapshot — единственный мост таксономии в pipeline**: pipeline-код не импортирует Django.

---

## Verification plan

### Phase 0
```bash
# Перед миграцией — снимок текущего состояния
docker-compose exec web python manage.py shell -c "
  from apps.catalog.models import App, AppPlatform
  from apps.sources.models import Source
  mcp_apps = App.objects.filter(sources__source_type='mcp_registry').distinct()
  print(f'MCP apps: {mcp_apps.count()}')
  print(f'  with AppPlatform: {mcp_apps.filter(platforms__slug=\"mcp\").count()}')
  print(f'  marked OFFICIAL: {mcp_apps.filter(platform_verification_status=\"official\").count()}')
"
# Запуск починенного ingest
docker-compose exec web make ingest
# Запуск backfill миграции
docker-compose exec web python manage.py migrate
# Повторный замер — все MCP apps должны иметь AppPlatform и OFFICIAL
```

### Phase 1
```bash
# Unit tests
docker-compose exec web pytest tests/agent/ -v
# Dry-run на одну существующую DRAFT
docker-compose exec web python manage.py agent_run --enrich-app=<slug> --dry-run
# Реальный батч на 3 апп
docker-compose exec web python manage.py agent_run --enrich-pending --limit=3
# Проверка результата + cost
docker-compose exec web python manage.py shell -c "
  from apps.agent.models import AgentRun, LLMCallLog, NeedsReviewQueueEntry
  latest = AgentRun.objects.latest('started_at')
  print(f'Run #{latest.pk}: cost=\${latest.total_cost_usd}')
  print(f'Queue entries created: {NeedsReviewQueueEntry.objects.filter(created_at__gte=latest.started_at).count()}')
"
```

### Phase 2
- Manual: открыть admin → NeedsReviewQueueEntry → проверить diff-preview, evidence-map, кнопки apply.

### Phase 3
```bash
docker-compose exec web python manage.py agent_run --source=rss --limit=5 --dry-run
docker-compose exec web python manage.py agent_run --source=github_mcp --limit=5 --dry-run
```

### Phase 4
```bash
docker-compose exec web python manage.py agent_reactualize --limit=3 --dry-run
# Проверить, что НЕТ записей в App.updated_at за последние 5 минут,
# но ЕСТЬ записи в NeedsReviewQueueEntry
```

### Phase 5
```bash
docker-compose exec web pytest tests/agent/eval/ --eval
```

### Production rollout (поэтапно)
- `AGENT_SOURCES_ENABLED=` пусто на старте.
- Phase 1 → включается через manage.py команды, не через beat.
- После Phase 2 → `AGENT_SOURCES_ENABLED=enrich_pending` (только обогащение существующих).
- После Phase 3 + production gate (см. выше) → `AGENT_SOURCES_ENABLED=enrich_pending,rss,github_mcp`.
- После direct-ingest pilot review → добавить `gemini_extensions,claude_connectors,chatgpt_apps`.
- После non-MCP enrichment review → если MCP enrichment ещё не одобрен
  отдельным бюджетом, убрать `enrich_pending` из `AGENT_SOURCES_ENABLED`
  или запускать только source-scoped ручные batches. Общий
  `enrich_pending` не различает MCP и non-MCP.

---

## Future: путь к автономному сервису

Когда понадобится true autonomous agent (planner-loop, self-directed research, кросс-источниковые задачи):

1. Извлечь `apps/agent/llm/`, `apps/agent/pipeline/`, `apps/agent/sources/` в отдельный репозиторий.
2. Заменить `apps/agent/persist.py` на HTTP-клиент к новому Django endpoint `POST /internal/agent/upsert-draft` (auth по HMAC-токену).
3. В новом сервисе использовать FastAPI + APScheduler ИЛИ Celery с собственным брокером.
4. Перенести `AgentRun`/`EnrichmentTask`/`LLMCallLog`/`NeedsReviewQueueEntry` в БД нового сервиса.
5. Главное Django-приложение продолжает владеть `App`/`Source`/`Submission` — агент становится "клиентом" каталога.

Эта траектория **не требует переделки pipeline-кода**, только смену граничного слоя (`persist.py`).
