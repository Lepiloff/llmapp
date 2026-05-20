# Multi-platform discovery — план реализации

Документ описывает план разблокировки **трёх дополнительных источников
данных** для каталога, найденных в research'е 2026-05-19 (см.
`agent-rollout-log.md` Sprint 4 prep).

**Implementation status на 2026-05-20:** Sprint 4 MVP реализован для
Gemini Extensions и Claude Connectors, плюс MCP Registry default URL
переведён на `/v0`. Локальный pilot/full-cycle validation пройден:
Gemini `--limit=30 --apply` записал 30 новых/обновлённых source rows,
Claude `--limit=5 --apply` записал 5 connectors; все новые карточки
остаются `draft/unreviewed/unknown` до явного editorial approve.

**Ожидаемый эффект:** каталог растёт с 24 опубликованных карточек
до ~2000-2500 в течение 2-4 недель после раскатки (с учётом editorial
throughput).

**Стоимость LLM:** **$0** для всех трёх новых источников. Это direct
ingest (JSON-feed для Gemini, HTML-парсинг для Claude, JSON-registry
для MCP) — никакого `enrich_new_app`-вызова, никакого budget hit.
Существующий GitHub MCP discovery остаётся LLM-enriched
(~$0.006/card) — без изменений. Раскатываем **поэтапно через
test-batches** (см. § Phased rollout) из-за DOM-drift риска и
editor-overload — не из-за бюджета.

## Содержание

1. [Контекст](#контекст)
2. [Target state](#target-state)
3. [Phased rollout — тест-батчи перед массовым ingest'ом](#phased-rollout--тест-батчи-перед-массовым-ingestом)
4. [Источник 1 — MCP Registry URL fix](#источник-1--mcp-registry-url-fix)
5. [Источник 2 — Gemini Extensions JSON-feed](#источник-2--gemini-extensions-json-feed)
6. [Источник 3 — Claude Connectors HTML crawler](#источник-3--claude-connectors-html-crawler)
7. [Источник 4 — ChatGPT Apps unofficial index](#источник-4--chatgpt-apps-unofficial-index)
8. [Cross-cutting changes](#cross-cutting-changes)
9. [Acceptance criteria](#acceptance-criteria)
10. [Risks + mitigations](#risks--mitigations)
11. [Repositioning (non-code)](#repositioning-non-code)

---

## Контекст

Архитектурный обзор от 2026-05-16 предполагал что **4 из 5 платформ
заблокированы по ToS**. Глубокий research 2026-05-19 это
**опровергнул**:

* **Claude:** `claude.com/robots.txt` явно разрешает crawl всем
  ботам (`User-Agent: *  Allow: /`). Anthropic Software Directory
  Terms регулируют **submission**, не **третьестороннюю агрегацию**.
* **Gemini Extensions:** Google публикует **полный JSON-feed** на
  `geminicli.com/extensions.json` (1026 extensions, 782KB, ежедневный
  refresh). Это официальный машинный endpoint.
* **MCP Registry:** не «лежит», а сменил версионирование с `/v1/` на
  `/v0/`. Наш Sentry-counter из Sprint 2 алертит впустую; правится
  одной env-переменной.
* **ChatGPT Apps:** официальный `chatgpt.com/apps` существует, но
  полноценный JSON/API feed не найден. Для MVP добавлен
  **неофициальный crawlable index** `mcpapp.net/chatgpt-apps`; OpenAI
  official/Playwright path остаётся отдельным hardening-вопросом.

Research-артефакты с конкретными цифрами и URL'ами — в
`agent-rollout-log.md` секция «Research 2026-05-19».

---

## Target state

После Sprint 4:

| Платформа | Источник | Cards (target) | Cadence |
|---|---|---|---|
| MCP | GitHub Search + MCP Registry v0 | 500-1000+ | daily (Registry) + 3×/week (GitHub) |
| Claude Connectors | claude.com/connectors HTML crawl | ~400 | weekly |
| Gemini Extensions | geminicli.com/extensions.json | ~1000 | daily |
| ChatGPT Apps | mcpapp.net/chatgpt-apps third-party crawl | ~250 | weekly/manual pilot |
| Gems / Enterprise | без источника | 0 | — |

`App.platforms` model и админ-UI уже готовы под все 5 — никаких
изменений в `apps/catalog/models.py`.

---

## Phased rollout — тест-батчи перед массовым ingest'ом

Не запускаем full ingest сразу. Не из-за LLM-бюджета (он у новых
источников = $0), а из-за **DOM/JSON-schema drift'а**,
**mapping-багов** и **editor-overload'а**. Сценарий накатывания:

### Phase A — Pilot (для каждого источника отдельно)

`manage.py agent_run --source=<name> --limit=30 --dry-run` → smoke
check без записи в БД. Затем `--apply` тех же 30 карточек.

**Что проверяется на 30 карточках:**

* **Mapping-полнота:** случайные 10 карточек — есть ли непустые
  `name` / `short_description` / `developer_url` / `repo_url`?
* **Capability evidence:** для Gemini — `hasMCP/hasContext/hasHooks`
  булевые честно мапятся в `AppCapability(value=yes, note="…")`. Для
  Claude — use case tags маппятся в наши Category либо валидно
  попадают в `unmapped_categories` payload.
* **Soft-duplicate hit rate:** сколько из 30 матчатся на existing
  cards через `find_soft_duplicate`? Если >50% — Gemini overlap с
  GitHub MCP discovery; ожидаемо.
* **Editorial sanity:** редактор open'ает `/admin/catalog/app/?status__exact=draft`,
  визуально прокликивает 5-10 карточек — выглядят ли они «как
  карточки в каталоге», а не как garbage.
* **Quality score после publish:** ставим editorial_review_status =
  reviewed, transition_to_published — карточка получает quality_score
  ≥ 40? Если нет — что-то критичное не заполнено.

**Acceptance для Phase A:** ≥80% карточек проходят visual sanity,
≥1 sample карточка успешно публикуется без правок.

**Если провал:** копаем причину — DOM regression / mapping bug /
incomplete JSON entry — фиксим, повторяем Phase A.

### Phase B — Scaled batch (по источнику отдельно)

После прохождения Phase A: `--limit=200 --apply`. Editor проходит
bulk-actions в админке (особенно «Approve & publish» для
high-confidence карточек: например `isGoogleOwned=true` у Gemini).

**Что проверяется на 200 карточках:**

* **Performance:** ingest < 5 минут (1 RPS rate-limit на claude.com
  означает ~6-7 минут для Claude). Если медленнее ожидаемого —
  где bottleneck (parsing? upsert? signals?).
* **Memory profile:** `docker stats` во время ingest'а — не растёт
  ли неограниченно?
* **Editorial throughput:** сколько часов editor занимает review
  200 карточек? Это даёт честную оценку capacity на full-scale.
* **Soft-dup ratio на масштабе:** если у Gemini 30% карточек
  оказались дубликатами MCP — это ожидаемо, не проблема. Если 80%
  — стоит подумать о `find_soft_duplicate` heuristics.

**Acceptance для Phase B:** processed/200, failed=0, editorial
sample (10 карточек) одобрен.

### Phase C — Full ingest

После Phase B всех трёх источников — включаем beat-расписание.

* Gemini: daily @ 04:30 UTC.
* Claude: weekly @ Tuesday 04:45 UTC.
* MCP Registry: уже daily @ 04:00 UTC (просто URL-fix).

Editor получает review digest @ 07:30 UTC ежедневно — нагрузка
теперь distributed, не one-shot 1400 cards.

### Контроль через flag-gating

Каждый источник управляется feature-flag'ом аналогично существующему
`AGENT_SOURCES_ENABLED=` (CSV). На Phase A — flag пустой, источник
доступен только через `manage.py --apply`. На Phase C — flag
добавляется в `.env`, beat начинает гонять.

Pilot-команды (без flag'а):
```bash
manage.py agent_run --source=gemini_extensions --limit=30 --dry-run
manage.py agent_run --source=gemini_extensions --limit=30 --apply
manage.py agent_run --source=claude_connectors --limit=30 --dry-run
manage.py agent_run --source=claude_connectors --limit=30 --apply
manage.py agent_run --source=mcp_registry --limit=30 --apply
```

Полная активация через env:
```bash
AGENT_SOURCES_ENABLED=github_mcp,rss,gemini_extensions,claude_connectors
```

(MCP Registry уже в beat по умолчанию, отдельный flag не нужен.)

---

## Источник 1 — MCP Registry URL fix

**Цена:** 5 минут.

### Изменения
* `.env.production` и `.env.example`:
  `MCP_REGISTRY_BASE_URL=https://registry.modelcontextprotocol.io/v0`
  (было `…/v1`).
* Перезапуск worker'а / beat'а.

### Проверка
* `manage.py shell` →
  `from apps.sources.tasks import ingest_mcp_registry; ingest_mcp_registry()`
  — counters > 0.
* Sentry-issue `mcp-registry-unreachable` перестаёт расти.

### Регрессионный тест
В `tests/sources/test_mcp_registry_sentry.py` тест 404→Sentry
сохраняем как есть (защита от будущих regression'ов). Дополнительно
добавить smoke `test_v0_endpoint_returns_drafts` — фикстурный mock
JSON от `/v0/servers`, проверка что `MCPRegistrySource.iter_drafts`
парсит.

---

## Источник 2 — Gemini Extensions JSON-feed

**Цена:** ~1 рабочий день. Самый дешёвый и крупный источник.

### Endpoint
`GET https://geminicli.com/extensions.json` → 200, JSON list[1026].

### Структура entry
```json
{
  "id": "obra-superpowers",
  "url": "https://github.com/obra/superpowers",
  "fullName": "obra/superpowers",
  "repoDescription": "An agentic skills framework...",
  "stars": 197771,
  "lastUpdated": "2026-05-19T13:03:16Z",
  "extensionName": "superpowers",
  "extensionVersion": "5.1.0",
  "extensionDescription": "Core skills library: TDD, debugging…",
  "avatarUrl": "https://avatars.githubusercontent.com/u/45416?v=4",
  "hasMCP": false,
  "hasContext": true,
  "isGoogleOwned": false,
  "licenseKey": "mit",
  "hasHooks": true,
  "hasSkills": true,
  "hasCustomCommands": false
}
```

### Маппинг на `AppDraft`

| AppDraft поле | Source entry |
|---|---|
| `name` | `extensionName` (fallback на repo name из `fullName`) |
| `slug_hint` | `slugify(extensionName)` |
| `short_description` | `extensionDescription` (≤280 char) |
| `long_description` | `repoDescription` |
| `developer_name` | первая часть `fullName.split('/')[0]` |
| `developer_url` | `f"https://github.com/{developer_name}"` |
| `official_page_url` | `url` (это GitHub repo) |
| `repo_url` | `url` |
| `platforms` | `["gemini"]` (всегда; entry с `hasMCP=true` → также `+["mcp"]`) |
| `listing_types` | `["gemini-extension"]` (новый листинг-тип, добавить в `seed.json`) |
| `capabilities` | derive из булевых: `hasMCP`, `hasContext`, `hasHooks`, `hasSkills`, `hasCustomCommands` (каждое → `Capability` row, value=yes/no с evidence «manifest declares hasX:true») |
| `external_id` | `f"gemini:{id}"` |
| `raw_payload` | весь entry as-is для audit |
| `platform_metadata` | `{"extension_version": …, "google_owned": isGoogleOwned, "rank": rank}` |

### Файлы для создания

* `apps/agent/sources/gemini_extensions.py` — класс
  `GeminiExtensionsSource(BaseSource)`. Метод `iter_drafts()` тянет
  JSON через `requests` (с `get_default_limiter().acquire(url)`),
  итерирует записи, мапит через хелпер `_entry_to_draft`.
* `apps/agent/tasks.py` — новая Celery-таска
  `ingest_gemini_extensions()` по образцу `ingest_mcp_registry`.
* `config/settings/base.py::CELERY_BEAT_SCHEDULE` — entry
  `ingest_gemini_extensions` daily @ 04:30 UTC (после link-checker, до
  re-actualization).
* `apps/catalog/fixtures/seed.json` — новый `ListingType(slug='gemini-extension')`,
  новый `Platform(slug='gemini', public_path='gemini-apps')` если
  ещё нет (платформа `gemini` существует, но проверить
  `public_path`).
* `tests/agent/test_gemini_extensions_source.py` — 6-8 тестов:
  - happy path: fixture JSON → expected `AppDraft` list.
  - field mapping: `hasMCP=true` → platforms == `["gemini","mcp"]`.
  - evidence-required: `hasContext=true` → `AppCapability(value=yes, note="…")`.
  - URL fetch failure (HTTPError) → graceful (zero counters, no crash).
  - dedup при повторном ingest: `external_id` matches → `outcome="skipped"`.

### Нюансы
* **No LLM cost** — это direct ingest, не enrichment. Каталог растёт
  без бюджетных рисков.
* **1026 cards за один ingest** — это много. `upsert_app_from_draft`
  обрабатывает одну за раз; нужно time-budget'нуть batch или
  стримить через `iterator()`. Прикинуть: 1026 × 50ms ORM-call = ~50
  секунд. Приемлемо без батчевания.
* **GitHub-overlap:** часть Gemini extensions — это **те же** MCP
  servers что мы дискаверим через GitHub. `find_soft_duplicate`
  по `repo_url` должен соединять; следить за growth ratio
  «new vs skipped» в первом run'е.

---

## Источник 3 — Claude Connectors HTML crawler

**Цена:** ~2 рабочих дня. Тонкий момент — HTML-парсинг + pagination.

### Endpoint
`GET https://claude.com/connectors` — Next.js SSR-страница, 597KB
HTML, ~14 страниц пагинации (398 connector'ов).

### Структура страниц
* Index: `claude.com/connectors` с `?page=1`…`?page=14` (проверить
  реальный URL-pattern на live-сайте).
* Per-connector: `claude.com/connectors/<slug>` (предположительно;
  верифицировать).

### Что парсить
Из card в DOM:
* name + logo URL
* short description (1-2 предложения)
* compatible products: Claude / Claude Code / Skills
* use case categories (массив тегов)
* publication date

Из detail-страницы:
* developer name
* developer URL (если указан)
* official URL для подключения
* long description
* capabilities (если указаны явно — read/write hints, OAuth scopes)

### Маппинг на `AppDraft`

| AppDraft поле | Source |
|---|---|
| `name` | card name |
| `slug_hint` | URL slug |
| `short_description` | card description |
| `long_description` | detail page description |
| `developer_name` | detail page «By X» |
| `developer_url` | detail page link |
| `official_page_url` | detail page primary URL |
| `platforms` | `["claude"]` |
| `listing_types` | `["claude-connector"]` (новый — добавить в seed) |
| `categories` | mapped из use case tags через таблицу translation (см. ниже) |
| `external_id` | `f"claude:{slug}"` |
| `raw_payload` | dict с raw HTML excerpts + card metadata |
| `platform_metadata` | `{"compatible_products": [...], "use_case_categories": [...], "publication_date": "..."}` |

### Соответствие use-case categories наших Category
Anthropic-side таги: AI/ML, Automation, Calendar, Cloud, CMS,
Communication, Customer Support, Data & Analytics, Design, Desktop
Automation, Development Tools, Documents, Education, Entertainment,
Finance, Government, Healthcare, Jobs, Legal, Lifestyle, Marketing,
Observability, Productivity, Project Management, Research, SAP,
Security, SEO, Ticketing, Travel.

Наши Category (см. `seed.json`): Productivity, Developer Tools, Research,
Files, Communication, Education, Creative, Data, Business,
Entertainment.

Маппинг должен быть явный в коде (`_ANTHROPIC_CATEGORY_MAP`); неизвестные
теги → `Source.payload['unmapped_categories']` для editor review.

### Файлы для создания

* `apps/agent/sources/claude_connectors.py`:
  - класс `ClaudeConnectorsSource(BaseSource)`.
  - `iter_drafts()` тянет index-pages пагинированно, для каждой
    карточки → fetch detail-page (с rate-limit через
    `get_default_limiter`), парсит BeautifulSoup'ом, выдаёт `AppDraft`.
  - **robots.txt compliance:** перед первым fetch'ем проверить что
    `https://claude.com/robots.txt` всё ещё содержит
    `User-Agent: *` `Allow: /` — добавить хелпер
    `_assert_robots_allows(url)` который кэширует robots.txt на
    час и валидирует path.
  - **User-Agent:** `LLMAppMarket-Agent/1.0 (+https://llmappmarket.com/bots)`
    (мы уже используем эту строку — единый identifier).
* `apps/agent/tasks.py` — таска `ingest_claude_connectors()` weekly
  @ Tuesday 04:45 UTC (после link-checker, до Gemini-ingest которая
  daily).
* `config/settings/base.py::CELERY_BEAT_SCHEDULE` — entry
  `ingest_claude_connectors` weekly.
* `apps/catalog/fixtures/seed.json` — `ListingType(slug='claude-connector')`,
  убедиться что `Platform(slug='claude')` корректный
  (есть `public_path='claude-connectors'`).
* `tests/agent/test_claude_connectors_source.py`:
  - fixture HTML (5-10 minified фрагментов от реальных страниц,
    зафиксированных через `curl` в snapshot).
  - happy path: index-page HTML → list of card stubs.
  - detail page HTML → full `AppDraft`.
  - robots.txt fail-safe: если `/robots.txt` отдаёт Disallow на
    `/connectors`, source выходит с warning и пустыми drafts.
  - unmapped category: tag «Healthcare» (нет в нашей таксономии) →
    попадает в `payload.unmapped_categories`, не блокирует draft.
  - rate-limit: смок-test что `get_default_limiter().acquire(url)`
    вызывается перед каждым fetch'ем.

### Dependency: BeautifulSoup
В `pyproject.toml` уже **отсутствует** — добавить:
```toml
"beautifulsoup4>=4.12",
"lxml>=5.0",        # парсер для bs4
```
Docker-build CI job (Sprint 3 follow-up) поймает если deps дрейфнут.

### Нюансы
* **HTML может поменяться** в любой момент — Anthropic не обязуется
  держать DOM-структуру. Mitigation: snapshot-тесты с реальным HTML;
  Sentry-alert если `iter_drafts` выдаёт <50% expected cards
  (намек на DOM regression).
* **Polite-client:** наш `DomainRateLimiter` уже cross-process через
  Redis (Sprint 1 follow-up). Дефолт 1 RPS — означает 398 cards × 2
  (index + detail) ≈ 13 минут на один полный crawl. Weekly cadence
  это терпит.
* **Каталог 1+1 fetch'а:** index-page имеет краткую инфу, detail —
  расширенную. Можно сделать в один проход (index только) для MVP,
  detail отложить.

---

## Источник 4 — ChatGPT Apps unofficial index

**MVP status на 2026-05-20:** реализован без Playwright.

Источник: `https://mcpapp.net/chatgpt-apps` — сторонний публичный
каталог, который индексирует ChatGPT Apps и отдаёт SSR HTML/detail
страницы без логина. `robots.txt` разрешает `User-Agent: *` для этих
страниц, запрещая только admin/API/import paths.

### Реализация
* `apps/agent/sources/chatgpt_apps.py` — `ChatGPTAppsSource`.
* `Source.SourceType.CHATGPT_UNOFFICIAL` — source row честно помечен
  как third-party discovery, не как OpenAI official feed.
* `manage.py agent_run --source=chatgpt_apps --limit=N [--apply]`.
* Beat entry: weekly Wednesday 04:45 UTC, gated через
  `AGENT_SOURCES_ENABLED=chatgpt_apps`.
* Mapping:
  - `platforms=["chatgpt"]`, плюс `claude` если mcpapp card помечает
    Claude surface.
  - `listing_types=["chatgpt-app"]`, плюс Claude listing type при
    multi-surface card.
  - `official_page_url/install_url` берутся из `chatgpt.com/apps/...`
    connect link, если он есть.
  - `raw_payload.source_kind="third_party_chatgpt_apps_index"`.

### Ограничения
* Это не официальный OpenAI feed. Карточки всегда остаются
  `draft/unreviewed/unknown` до редактора.
* Soft-duplicate на существующую карточку создаёт дополнительный
  `Source` row; объединение платформ остаётся editorial decision.
* OpenAI official/partner channel и Playwright route можно добавить
  позже как separate source после ToS/legal review.

---

## Cross-cutting changes

### Beat-расписание (новые entries)
```python
"ingest_gemini_extensions": {
    "task": "apps.agent.tasks.ingest_gemini_extensions",
    "schedule": crontab(hour=4, minute=30),
},
"ingest_claude_connectors": {
    "task": "apps.agent.tasks.ingest_claude_connectors",
    "schedule": crontab(day_of_week="tue", hour=4, minute=45),
},
```

### Settings / env
* `MCP_REGISTRY_BASE_URL` — `/v1` → `/v0` (см. источник 1).
* `GEMINI_EXTENSIONS_URL` (новый, default `https://geminicli.com/extensions.json`)
  — env override для тестов и на случай если Google поменяет URL.
* `CLAUDE_CONNECTORS_BASE_URL` (новый, default `https://claude.com/connectors`).

Все новые env-переменные документировать в `.env.example` и
`.env.production`.

### seed.json
Добавить два `ListingType`:
* `claude-connector` — «Claude Connector»
* `gemini-extension` — «Gemini Extension»

Уже существуют (verified):
* `Platform(slug='claude', public_path='claude-connectors')` ✅
* `Platform(slug='gemini', public_path='gemini-apps')` — проверить
  `public_path`.

### Docs
* `agent-pipeline.md`:
  - Phase 3 Discovery: добавить `gemini_extensions` и `claude_connectors`
    в список sources.
  - Phase 4 prerequisites: ToS-blocked entry для ChatGPT Apps
    переписать («headless-route выполним; partner-channel preferred»).
  - Critical files: добавить новые `apps/agent/sources/gemini_extensions.py`
    + `claude_connectors.py`.
* `agent-rollout-log.md`: новая секция «Sprint 4 — multi-platform
  unblock (date TBD)».
* `pre-launch-checklist.md`: фаза 3.1 «B3 ToS» переписать —
  больше не блокер, переоценка после research'а 2026-05-19.
* `project-overview-ru.md`: § «Что это» repositioning (см. ниже).

---

## Acceptance criteria

Sprint 4 проходит через Phase A → B → C (см. § Phased rollout). Gate
для перехода между phase'ами:

### Phase A → B (по каждому источнику отдельно)

1. **(quantitative)** `agent_run --source=<X> --limit=30 --apply`
   создаёт ≥25 DRAFT-карточек (≥83% success rate из 30).
2. **(qualitative)** Editor визуально одобряет 10 random samples в
   `/admin/catalog/app/?status__exact=draft` — карточки «выглядят как
   карточки», не garbage.
3. **(qualitative)** ≥1 sample карточка успешно прошла
   `transition_to_published` без ручного редактирования полей и
   получила `quality_score ≥ 40`.
4. **(qualitative)** Для Gemini: random 10 карточек — `hasMCP /
   hasContext / hasHooks` булевые честно мапятся в
   `AppCapability(value=yes, note="manifest declares …")`.
5. **(qualitative)** Для Claude: random 10 карточек — `developer_name
   / developer_url / official_page_url` непустые; use case tags
   маппятся либо в нашу таксономию, либо валидно попадают в
   `Source.payload.unmapped_categories`.

### Phase B → C (по каждому источнику отдельно)

6. **(quantitative)** `--limit=200 --apply` отрабатывает за <8 минут
   с failed=0.
7. **(operational)** Memory: `docker stats` worker'а — RSS не растёт
   неограниченно (cap <500MB во время ingest'а).
8. **(qualitative)** Editorial sample-review на 10 карточках из
   batch'а 200 — одобрен (минимум 7/10 пригодны к публикации без
   правок).

### Sprint 4 completion (overall)

**MVP status на 2026-05-20:** implemented locally for Gemini/Claude
pilot batches. Полный Phase B/C на больших batch'ах и production-stack
API checks остаются rollout-задачами перед включением beat в проде.

9. **(quantitative)** MCP Registry ingest возвращает ≥10
   new/updated drafts на первом run'е после URL-fix'а (proves `/v0/`
   working).
10. **(operational)** Все unit-тесты `tests/agent/` зелёные.
11. **(operational)** CI docker-build job зелёный (deps в pyproject
    совпадают с image'м).
12. **(operational)** На реальном prod-стэке после Phase C первого
    источника: `curl /api/v1/apps/?platform=gemini&page_size=10`
    возвращает ≥10 результатов; то же для `platform=claude`.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Gemini extensions JSON format меняется без warning'а | Pydantic-валидация полей в `GeminiExtensionsSource._parse_entry`; неизвестные поля → лог + `Source.payload.unparsed_keys` для audit; Sentry-alert если ≥10% записей не парсятся |
| Claude HTML DOM меняется | snapshot-тесты + Sentry-alert на «iter_drafts() < expected_min_cards» (50% threshold) |
| robots.txt у Anthropic меняется на Disallow | Перед каждым crawl проверять live `robots.txt`; если запрещён — source skip'ает run, шлёт Sentry-warning |
| 1400 новых DRAFT перегружают editor | На старте `AGENT_REVIEW_DIGEST_EMAILS` получает digest со всеми; editor может **bulk-approve** через admin action. Альтернатива: `AppPlatformVerificationStatus` для гemini/claude карточек выставляется в `unknown` (не `official`) — публикация требует явного editor-approval |
| Soft-duplicate между Gemini Extension и существующим MCP server | `find_soft_duplicate` уже работает по `repo_url`/`developer_url` (Sprint 3 swap на `difflib`); ingest второго источника просто создаст дополнительный `Source` row, не дублирующий `App` |
| Cost LLM на enrichment 1400 карточек | **Нет.** Эти источники — **direct ingest**, не enrichment. LLM не вызывается. Бюджетный latch не срабатывает |
| Editorial-throughput блокирует launch | Pre-launch-checklist Phase 0.7 «Editorial роль» — определить до Sprint 4 ingest'а |

---

## Repositioning (non-code)

После Sprint 4 — обновить публичные тексты:

### `project-overview-ru.md` § 1 «Что это»
> Сейчас: «Каталог приложений на базе LLM: GPT-Apps, Claude Connectors,
> MCP Servers, Gemini Apps и enterprise-агенты»

> Stать: «Cross-platform discovery layer для **MCP servers**, **Claude
> Connectors** и **Gemini Extensions** — три открытые экосистемы LLM-приложений,
> агрегированные в один searchable catalog. ChatGPT Apps Directory —
> Q3 2026 (после partner-clearance). Enterprise-agents — out of
> initial scope.»

### `templates/base.html` (hero на главной)
Сейчас: «Discover apps, connectors and agents for ChatGPT, Claude,
Gemini and beyond.»

Стать: «Discover MCP servers, Claude Connectors, and Gemini Extensions —
the open layer of the LLM ecosystem.»

### Marketing tagline (если есть)
Differentiation от Glama / PulseMCP: **«The only catalog that covers
all three open LLM platforms.»**

### Submissions page
Сделать explicit что ChatGPT App developer'ы тоже могут заявить
карточку через `/submit/` — это закрывает 5th-platform gap до
момента когда headless-crawler созреет.

---

## Estimated effort summary

| Item | Dev | Editorial |
|---|---|---|
| MCP Registry URL fix + smoke | 5 мин | — |
| Gemini Extensions source + tests | 1 день | — |
| Claude Connectors source + tests | 2 дня | — |
| Cross-cutting (beat, seed, docs) | 0.5 дня | — |
| Phase A pilots (3 источника × 30 cards) | 1 час прогон | 1-2 часа sample review |
| Phase B scaled (3 источника × 200 cards) | 2 часа прогон | 4-6 часов review |
| Phase C full ingest + monitoring | beat-driven | distributed по дням |
| **Sprint 4 dev total** | **~3.5 дня** | **~1 неделя editorial** |
| ChatGPT Apps unofficial source | 0.5 дня | sample review |

**Editorial-bottleneck — главная зависимость.** Technical-side
готов поставить 1400+ DRAFT'ов в течение часов; editor реалистично
обрабатывает ~50-100 карточек/день при среднем темпе. Полный launch
Phase C распределяет нагрузку через daily/weekly beat — это
sustainable.

**LLM-бюджет:** $0 для Sprint 4. Existing $20/мес budget идёт только
на GitHub MCP enrichment, которое не масштабируется ingest'ом
Gemini/Claude (там нет LLM-вызовов).

---

## Open questions (решить до старта Sprint 4)

1. **Bulk-publish для Gemini-Extensions?** На входе у нас 1026
   карточек, многие — high-quality (Google-maintained). Разрешить
   editor'у bulk-approve по фильтру `isGoogleOwned=true`?
2. **Soft-duplicate strategy при overlap'е MCP↔Gemini.** Сейчас
   создаётся дополнительный `Source`; должна ли карточка показывать
   обе платформы в `App.platforms`? (Default — да, через
   `attach_platforms`.)
3. **Initial vs subsequent ingest cadence.** Первый ingest = 1026
   cards. Subsequent — только delta. Нужен `last_seen` tracking в
   `Source.payload` чтобы не процессить unchanged extensions
   каждый день.
4. **Bot-disclosure page** на `https://llmappmarket.com/bots` — мы
   используем этот URL в User-Agent с самого Sprint 1, но страница
   до сих пор не создана. Pre-Sprint-4 — создать минимальную
   страницу: «We crawl publicly available catalog data from … to
   build a cross-platform discovery service. Contact …». Защищает
   нас в случае ToS-разговора с Anthropic / Google.
