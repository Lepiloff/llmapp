# LLM App Market — обзор для владельца проекта

Документ передаёт понимание проекта на уровне "что система делает,
для кого, какую ценность создаёт, как устроено внутри". Технических
деталей до уровня классов и методов нет — только компоненты, потоки
данных и точки контроля.

---

## 1. Что это

**LLM App Market** — публичный каталог приложений на базе больших
языковых моделей: GPT-Apps, Claude Connectors, MCP Servers, Gemini
Apps и enterprise-агенты. Сайт работает как "Product Hunt + App Store
для LLM-экосистемы": посетитель ищет, что подключить к своему ChatGPT
или Claude, и попадает на структурированную карточку с описанием,
ссылками, отзывами капабилити и кросс-ссылками на похожее.

Главная страница: `https://llmappmarket.com`.

---

## 2. Кому это нужно и какую боль решает

**Конечный пользователь** (B2C):
- Активные пользователи ChatGPT / Claude / Gemini, которые ищут чем
  расширить функциональность своего ИИ
- Разработчики, исследующие что уже построено вокруг MCP протокола
- Editorial-журналисты пишущие про инструменты ИИ

**Боль которую закрываем:** официальные директории ChatGPT / Claude
фрагментированы, нет единой точки поиска "что есть для X на любой
платформе". В Reddit-постах и Medium-статьях рекомендации устаревают
за месяц. Наш каталог:
- объединяет 5 платформ в одну модель данных
- LLM-pipeline сам подтягивает свежие карточки и периодически их
  пере-актуализирует
- редактор только проверяет/публикует, не пишет копирайт с нуля

**Партнёр / разработчик** (B2B):
- Возможность заявить права на свою карточку (`ClaimRequest`)
- В будущем — featured-размещения и аналитика по installs

---

## 3. Текущее состояние (на 2026-05-16)

| Метрика | Значение |
|---|---|
| Опубликованных приложений | 24 |
| Платформ | 5 (ChatGPT, Claude, Gemini, MCP, Enterprise) |
| Категорий | 10 (Productivity, Developer Tools, …) |
| LLM-стоимость текущего месяца | $0.20 (бюджет $20) |
| Per-published-app LLM cost | $0.006 (на каталог из 24 приложений) |
| Готовность к проду | ✅ зелёный свет (см. `docs/deployment-ru.md`) |

---

## 4. Что видит конечный пользователь

### Публичные страницы

| URL | Назначение |
|---|---|
| `/` | Главная: "Trending now" (по analytics-скорам) + "Fresh in the grid" (последние published) |
| `/apps/` | Полный листинг с пагинацией |
| `/apps/?q=mcp` | Поиск (Postgres full-text + trigram fuzzy для опечаток) |
| `/apps/<slug>/` | Карточка приложения: long description, capability evidence quotes, similar tools, ссылки официальная/install/repo |
| `/apps/<category-slug>/` | Категория: все приложения внутри (например `/apps/developer-tools/`) |
| `/chatgpt-apps/`, `/claude-connectors/`, `/gemini-apps/`, `/mcp-servers/`, `/enterprise-agents/` | Платформенные хабы — все приложения данной платформы |
| `/submit/` | Форма "submit your app" с Cloudflare Turnstile-капчей |
| `/blog/` | Editorial-статьи (опционально, ручной контент редактора) |
| `/newsletter/` | Подписка на weekly digest |
| `/sitemap.xml`, `/robots.txt` | SEO |
| `/go/<app-slug>/` | Click-tracking redirect (для аналитики кликов по install/official URLs) |

### Что НЕ показывается публично

- Карточки в статусе `DRAFT` — даже если на них пришёл прямой URL
- Карточки в статусе `HIDDEN` (вручную скрытые редактором)
- Capabilities со значением `unknown`
- Раздел "platform-availability" если платформа не verified
- LLM-сгенерированный verdict до того как редактор его проверил
  (он живёт в `Source.payload` и виден только в админке)

---

## 5. Что делает редактор (админка `/admin/`)

### Основные роли админки

**1. Editorial review queue** (`/admin/agent/needsreviewqueueentry/`)

Сюда попадает всё, что LLM-pipeline предложил, но не применил
автоматически. Три категории:

- `enriched` — обогащение DRAFT-карточки: LLM предложил category,
  listing-type, verdict, pricing model, capabilities. Редактор
  одним кликом применяет (или отклоняет) каждое предложение.
- `reactualized` — diff после периодической ре-актуализации
  опубликованной карточки. LLM пересмотрел README и предлагает
  обновить описание / verdict / launch_status. Редактор решает что
  применить.
- `vanished` — линк сдох (7 фейлов подряд на official/install URL).
  Карточка автоматически переведена в `launch_status=deprecated`,
  но редактор видит событие в очереди и может его подтвердить или
  откатить.

**2. Apps changelist** (`/admin/catalog/app/`)

Прямое редактирование карточек. Из админки доступны bulk actions:
"Approve & publish", "Mark as hidden", "Recalculate quality score".
Любое поле, которое редактор изменил вручную, агент больше никогда
не перепишет — это hard-инвариант пайплайна.

**3. Cost dashboard** (`/admin/agent/agentrun/cost-dashboard/`)

Одна страница: spend текущего месяца + бюджет + утилизация %,
trend по дням за 30 дней, разбивка по моделям и по source-type,
Top-10 самых дорогих запусков. Из неё одним кликом проваливаешься
в детали конкретного AgentRun.

**4. Budget state** (`/admin/agent/budgetmonthstate/`)

Одна строка на UTC-месяц. Содержит латчи "discovery_disabled" (80%) и
"hard_stop" (100%). Снять латч = очистить таймштамп вручную.

**5. Link health** (`/admin/sources/linkhealth/`)

Что сломалось в каталоге. Сортировка по `consecutive_failures`
показывает кандидатов на ручной разбор до того как сработает
auto-deprecate.

### Команды управления (CLI)

| Команда | Что делает |
|---|---|
| `agent_run --enrich-app=<slug>` | Прогнать LLM-обогащение одной DRAFT-карточки (dry-run по умолчанию, `--apply` для записи) |
| `agent_run --source=github_mcp --limit=10` | Прогнать discovery вручную (например после изменения промпта) |
| `agent_run --enrich-pending --limit=5` | Walk через все DRAFT'ы не получившие обогащения |
| `agent_phase3_report` | Снимок состояния каталога: сколько published, approval rate, total cost |
| `agent_backfill_costs` | Пересчитать `LLMCallLog.cost_usd` после изменения цены модели у поставщика |
| `seed_demo` | Пересеять reference-данные (платформы, категории, capabilities) — идемпотентно |

---

## 6. Как наполняется каталог (LLM-pipeline / "Agent")

Pipeline разбит на 5 фаз; все они уже реализованы и проходят
end-to-end. Цель — в идеале редактор только нажимает "Approve",
а карточка из обычного GitHub-репозитория автоматически приходит
в каталог с правильным описанием, категорией, capability-флагами и
ссылками.

### Phase 0 — Ingest из MCP Registry

Источник: официальный MCP Registry (`registry.modelcontextprotocol.io`).
Раз в сутки Celery-задача `ingest_mcp_registry` опрашивает registry,
парсит JSON-каталог, для каждого нового сервера создаёт `Source`
строку и DRAFT-карточку в `App`. Никаких LLM-вызовов на этом этапе.

> **Особенность:** на 2026-05-16 endpoint MCP Registry изменился и
> возвращает 404. Задача обрабатывает это gracefully (counters=0,
> no crash). Phase 3 (GitHub MCP search) — основной активный
> источник новых карточек.

### Phase 3 — Discovery (RSS + GitHub MCP search)

Два независимых источника, оба гоняются по расписанию в beat:

**RSS** (`discover_rss`, каждые 6 часов):
- Парсит RSS/Atom-ленты блогов: Anthropic, OpenAI, Google AI, плюс
  GitHub Topic-feed для `model-context-protocol`
- Каждая найденная ссылка отправляется в **cheap LLM** (gpt-5.4-nano)
  с промптом `discover-v1.0` — модель отвечает YES/NO "является ли
  это релевантной карточкой для каталога" + canonical_url
- Релевантные кандидаты идут на enrichment

**GitHub MCP search** (`discover_github_mcp`, Пн/Ср/Пт в 06:30 UTC):
- Использует GitHub Search API по topic'у `mcp-server`
- Для каждого репо — `Contents API` для README через base64
- Дальше тот же путь через cheap LLM для классификации

**Что важно про cheap LLM:** на этом этапе вызовы дешёвые (~$0.0001
на кандидата), отсев большой (типично 30-60% кандидатов отклоняется
как нерелевантные). Это защищает от трат primary-модели на мусор.

### Enrichment — primary LLM пишет полное описание

После того как cheap LLM сказал YES, в дело идёт **primary LLM**
(gpt-5.4-mini, ~$0.006 на карточку) с промптом `enrich-new-v1.0`:

Вход: текст README / страницы продукта + текущая taxonomy каталога
(список доступных платформ, категорий, listing-types, capabilities).

Выход (`EnrichedDraft`):
- `name`, `short_description`, `long_description`
- `developer_name`, `developer_url`, `official_page_url`, `repo_url`, `install_url`
- `listing_types` (с confidence-score'ом для каждого)
- `categories` (с confidence)
- `capabilities` (yes/no/unknown + обязательная цитата-evidence для yes/no)
- `use_cases` (3-7 verb-led фраз)
- `proposed_verdict` (one-liner для редактора)
- `scope_summary`, `launch_status`, `pricing_model`

**Hard-контракт:** LLM никогда не пишет напрямую в `App.status`,
`App.editorial_review_status`, `App.platform_verification_status`,
`App.verdict`. Эти 4 поля — собственность редактора. LLM предлагает,
редактор применяет через очередь.

**Hard-контракт capability:** "yes/no" допустимо только если есть
текст-evidence из источника. Без evidence система автоматически
понижает до `unknown`. Это защита от галлюцинаций — нельзя
проставить "поддерживает удаление" если в README такого утверждения
нет.

### Editorial review (Phase 2)

DRAFT-карточка с обогащением и `NeedsReviewQueueEntry` ждёт
редактора. Редактор открывает её в админке, видит:
- Текущее состояние полей карточки
- Что LLM предложил (с цитатами-evidence)
- Confidence score'ы для категорий
- Цена и токены конкретного LLM-вызова
- Audit trail: какой run, какой prompt-version, какая модель

И принимает решение: `Apply proposed verdict` / `Approve & publish` /
`Reject` / `Mark resolved`. Bulk-actions работают по списку из
changelist'а.

### Phase 4 — Re-actualization (поддержка свежести)

Опубликованная карточка раз в 30 дней (по умолчанию) идёт через
**ре-актуализацию**: pipeline берёт первичный `Source`, выкачивает
README заново, прогоняет через primary LLM в режиме enrich-new и
считает **diff** против текущего состояния `App`.

Если что-то изменилось — текст, capability flipped с `yes` на `no` с
новой evidence, появилась новая категория — в `NeedsReviewQueueEntry`
с `kind=reactualized` падает запись с полным diff'ом. Редактор
решает применить.

**Что НЕ пишется автоматически даже здесь:** Re-actualization это
*check-in*, не enrichment. Опубликованные карточки — собственность
редактора. Pipeline обновляет только три вещи без спросу:
- `Source.last_enriched_at` (чтобы knew когда следующая ре-актуализация)
- `Source.payload` (audit-trail)
- `LinkHealth` (доступность URL'ов)

Beat расписание — ежедневно в 07:00 UTC, batch размером
`AGENT_REACTUALIZATION_BATCH_SIZE` (по умолчанию 20). Apps выбираются
NULLS FIRST: те, которые ни разу не ре-актуализировались, идут первыми.

### Vanish detection (защита от мёртвого каталога)

`check_app_links_batch` (ежедневно в 05:00 UTC) опрашивает все
published-карточки HEAD-запросом. 4 типа URL: official, install,
repo, directory. Каждый результат пишется в `LinkCheckResult`,
агрегат в `LinkHealth`.

Когда official или install URL **7 раз подряд** возвращает не-2xx:
- `App.launch_status` → `deprecated`
- `Source.is_active=False` для строк где `source_url` == сдохший URL
- В очередь падает `NeedsReviewQueueEntry(kind=vanished)` —
  чтобы редактор подтвердил или откатил автоматическое снятие

Vanish-событие срабатывает **ровно один раз** на каждое пересечение
порога. Если URL восстановился — счётчик обнулится, при новом
"умирании" будет новое событие в очереди.

### Phase 5 — Бюджетный hard-stop

Без контроля бюджета LLM-pipeline может за ночь съесть несколько
сотен долларов на API. Защита из двух порогов:

**80%** от `AGENT_MONTHLY_BUDGET_USD` (по умолчанию $20):
- Discovery beat (`discover_rss`, `discover_github_mcp`) отключается
- Re-actualization и enrichment продолжают работать (это рутинная
  поддержка существующего каталога — важнее чем добавление новых)
- На email из `AGENT_BUDGET_ALERT_EMAILS` уходит письмо

**100%**:
- Любой агент-вызов отказывается стартовать через `assert_agent_can_run`
- Письмо уходит снова с темой "100% reached"
- Латч стоит до конца месяца ИЛИ пока редактор не очистит
  `hard_stop_at` в админке

Beat `agent_budget_check` бежит каждый час в `:15`, перепроверяет
spend и обновляет latches. Латч **залипает** — даже если spend
после пересчёта окажется ниже 80%, discovery не вернётся
автоматически до конца месяца (защита от flapping'а где после
автоснятия pipeline сразу взорвался бы заново).

---

## 7. Защиты и инварианты (что система НИКОГДА не делает)

Это контракты, на которых стоит вся pipeline. Они в коде и в
тестах одновременно — попытка нарушить их даёт runtime-исключение
или test failure.

| Инвариант | Где enforced |
|---|---|
| LLM не пишет в `App.status`, `editorial_review_status`, `platform_verification_status`, `developer_claim_status`, `App.verdict` | `apps/agent/persist.py::_FORBIDDEN_FIELDS` + `tests/agent/test_persist.py` |
| LLM не перезаписывает уже заполненные текстовые поля | `apps/agent/pipeline/merge.py` (apply только если current value пустой) |
| Capability=yes/no только с evidence | `apps/agent/pipeline/validate.py` (downgrade в `unknown` без evidence) |
| Re-actualization вообще ничего не пишет в App-поля | `apps/agent/persist.py::queue_reactualization` — пишет только в `NeedsReviewQueueEntry` |
| Race-safety: между моментом snapshot'а и моментом записи редактор может править | `apply_merge_set` открывает `SELECT … FOR UPDATE` и пере-проверяет каждое поле; редактор всегда побеждает |
| Discovery `--apply` гэйтнут на `AGENT_SOURCES_ENABLED` | `apps/agent/tasks.py::_source_enabled` |
| Любой агент-вызов гэйтнут на бюджете | `apps/agent/tasks.py` — `assert_agent_can_run()` в начале каждого orchestrator'а |

---

## 8. Прочие модули проекта

Помимо catalog + agent + sources, в проекте живут:

**`search`** — Postgres full-text search + trigram fuzzy match для
опечаток. `refresh_search_vectors_batch` (ежесуточно в 03:00 UTC)
пересобирает `search_index_text` всем published-карточкам.

**`seo`** — `rebuild_sitemap` (каждые 30 минут) генерирует sitemap.xml.
Подписаны на signal post-save опубликованной карточки, чтобы новые
карточки появлялись в sitemap'е почти сразу. `ping_search_engines`
дёргает Google и Bing после rebuild'а.

**`analytics`** — `calculate_trending_scores` (ежесуточно) считает
trending-скор для каждого приложения исходя из clicks-stats (через
`/go/<slug>/` redirects). На главной "Trending now" сортировка
именно по этим скорам.

**`newsletter`** — еженедельная подписка. `create_weekly_draft`
(каждую пятницу в 06:00 UTC) собирает топ-апы недели в `Issue` со
статусом `DRAFT`. Редактор просматривает и нажимает "Send" — тогда
`send_newsletter_issue` отправляет всем `Subscriber'ам`.
`EmailClick` + `EmailOpen` пишут aggregate-метрики делая редкие
hitов по `/go/...` URL'ам с уникальным трекером.

**`editorial`** — раздел `/blog/` под полностью ручной контент.
Никакого LLM-pipeline здесь нет.

**`submissions`** — публичная форма `/submit/` и модель `ClaimRequest`
для разработчиков, заявляющих собственность на карточку. Auto-check
для claim'ов реализован но базовый (проверяет наличие developer'а в
известных доменах).

**`core`** — `TimeStampedModel`, `/health/` endpoint, общие хелперы.

---

## 9. Что отложено или открыто

Документ `docs/agent-rollout-log.md` содержит полную история фаз и
findings. Из живого списка осталось два пункта:

**F1 — Anthropic provider as primary.** В коде стоит stub, который
бросает `NotImplementedError`. Отложено по политике "до прод-релиза
работаем на OpenAI; LLM-провайдер swap делаем после стабилизации".
Когда вернёмся: ~1-2 часа работы, реализуется по образцу
`OpenAIProvider`. Memory-нота: предполагалось что primary будет
Claude Sonnet когда переключим.

**B3 — Official directories scrapers.** ChatGPT App Directory,
Claude Connectors index, Gemini Apps directory. Заблокировано на
legal/ToS review. Когда unblock: пишем консервативные scrapers
(1 RPS/domain, robots.txt, identifying UA, Sentry alerts на сбой),
beat 3 раза в неделю. **Действие:** маршрутизировать на legal-owner
для проверки ToS платформ перед написанием кода.

---

## 10. Где смотреть метрики, если что-то пошло не так

| Симптом | Куда смотреть |
|---|---|
| Сайт лёг / 502 | `docker compose logs web --tail=200`, `/health/` |
| LLM-pipeline молчит | `/admin/agent/agentrun/` — последние runs со status=failed; `/admin/agent/budgetmonthstate/` — не висит ли hard_stop |
| Карточки стали пропадать с сайта | `/admin/sources/linkhealth/` — отсортировать по consecutive_failures; `/admin/agent/needsreviewqueueentry/?kind=vanished` |
| Editor digest не приходит | `/admin/agent/needsreviewqueueentry/` — есть ли в очереди что-то pending; `AGENT_REVIEW_DIGEST_EMAILS` в `.env` настроен корректно |
| Поиск тупит / пропускает приложения | `docker compose logs worker` — `refresh_search_vectors_batch` отрабатывает успешно |
| Расходы на LLM выросли неожиданно | `/admin/agent/agentrun/cost-dashboard/` — per-day trend; топ-10 expensive runs |
| Sitemap устарел | `docker compose logs worker --since=1h` — `rebuild_sitemap` бежит каждые 30 мин |

Sentry (если `SENTRY_DSN` настроен) ловит все exceptions из web и
worker — обычно первая точка куда смотреть на reproducible-проблемы.

---

## Резюме

Проект — это **публичный каталог LLM-приложений** с
полу-автоматическим pipeline'ом наполнения. Главная ценность —
объединение фрагментированной экосистемы (5 платформ) в один
findable-каталог, где карточки **сами** появляются из GitHub
(через discovery), **сами** обогащаются (через primary LLM с
evidence-контрактом), и **сами** проверяются на свежесть (через
re-actualization).

Роль редактора — финальная одобрение и тонкие правки. Роль системы
— защитить редактора от мусора (cheap-LLM фильтр, evidence-required,
confidence-floor) и от перерасхода ($20/мес латчи).

Готов к проду; список open items — F1 (Anthropic provider) и B3
(official directories), оба заблокированы внешними факторами, не кодом.
