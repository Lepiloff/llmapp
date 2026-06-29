# Production readiness: тестирование работоспособности под ключ

Дата документа: 2026-06-26.

Цель: довести production до режима, где каталог регулярно обновляется,
проверяет актуальность, готовит изменения к публикации и постепенно
переходит от ручной модерации к безопасной автоматизации. MCP enrichment
оставляем на последний этап: сначала должны быть проверены расписания,
лимиты, review flow и публикационный pipeline.

## Текущий статус

Источник истины по фактическому состоянию production: checkpoint в
[`docs/deployment-ru.md`](deployment-ru.md) от 2026-06-05.

Коротко:

- Production БД наполнена с нуля, без переноса локальной dev-БД.
- Direct-ingest выполнен по всем текущим платформам:
  - MCP Registry: 10 915 sources, сверено с API, `missing=0`.
  - Gemini Extensions: 1 050 sources.
  - Claude Connectors: 24 sources.
  - ChatGPT unofficial: 293 sources.
- Каталог содержит 11 952 draft-карточки, автопубликации не было.
- LLM enrichment выполнен только для non-MCP:
  - Gemini: 764/764 apps enriched.
  - Claude: 24/24 apps enriched.
  - ChatGPT: 288/288 apps enriched.
  - Non-MCP enrichment: 1 035 tasks persisted, failures=0.
- MCP enrichment полный не запускался:
  - MCP apps: 10 915.
  - MCP pending enrichment: 10 880.
  - Оценка полного MCP enrichment: около `$28.70`.
- Review queue после non-MCP enrichment: 1 030 pending proposals.

## Рабочий принцип

Bootstrap != регулярная актуализация.

Первичное наполнение было большим разовым действием. Дальше система должна
работать инкрементально:

- direct-ingest перечитывает источники и пишет через upsert без LLM;
- enrichment обрабатывает только новые/pending draft-карточки;
- re-actualization проверяет опубликованные карточки и создает diff для
  review queue;
- публикация и автоматическое принятие изменений должны быть отдельным
  управляемым слоем с аудитом, лимитами и rollback-путем.

## Стадии дальнейшей работы

### Stage 0 — Handoff и safety freeze

Цель: начать новый рабочий блок без случайного расхода бюджета и без
потери текущего состояния.

Задачи:

- Проверить `git status`, закоммитить или явно оставить незакоммиченные
  изменения docs.
- На production проверить `AGENT_SOURCES_ENABLED` и
  `AGENT_ENRICH_PENDING_SOURCE_TYPES` без вывода секретов.
- Если MCP enrichment пока не запускаем, убедиться что общий
  `enrich_pending` не будет бесконтрольно брать MCP pending: allowlist не
  должен содержать `mcp_registry` или `all`.
- Проверить health: `/health/`, состояние `web`, `worker`, `beat`,
  `postgres`, `redis`, `caddy`, `pg_backup`.
- Снять non-secret snapshot: counts по `App`, `Source`, `AgentRun`,
  `EnrichmentTask`, `NeedsReviewQueueEntry`, `LLMCallLog`, budget state.

Exit criteria:

- Понятно, какие beat-задачи реально включены.
- Нет активных ручных `agent_run`/`manage.py shell` процессов.
- Budget и feature flags не позволяют случайно запустить MCP enrichment.

### Stage 1 — Inventory scheduled tasks

Цель: понять полный список задач по расписанию и их blast radius.

Артефакт: [`docs/scheduled-tasks-inventory-ru.md`](scheduled-tasks-inventory-ru.md).

Задачи:

- Сверить `CELERY_BEAT_SCHEDULE` из `config/settings/base.py`.
- Сверить реальные задачи в `django_celery_beat`.
- Для каждой задачи описать:
  - расписание;
  - feature flag;
  - пишет ли в БД;
  - вызывает ли LLM;
  - ожидаемую стоимость;
  - безопасный dry-run или limited-run способ проверки;
  - таблицы/метрики для проверки результата.
- Разделить задачи на группы:
  - infrastructure: backup, sitemap, retention, health/budget;
  - catalog maintenance: search vectors, quality/trending, link checks;
  - direct ingest: MCP/Gemini/Claude/ChatGPT;
  - discovery: RSS/GitHub MCP;
  - enrichment;
  - re-actualization.

Exit criteria:

- Есть таблица scheduled tasks с expected result и rollback notes.
- Рискованные задачи помечены как `manual only` до отдельного решения.

### Stage 2 — Проверка безопасных operational задач

Цель: доказать, что базовая production эксплуатация работает.

Задачи:

- Проверить `pg_backup`: наличие свежего dump, retention, S3 upload если
  настроен.
- Проверить sitemap refresh и доступность sitemap URL.
- Проверить retention cleanup в dry-run/limited mode, если реализовано.
- Проверить search-vector refresh/rebuild на ограниченном наборе.
- Проверить budget check без искусственного превышения реального бюджета
  или с временным тестовым сценарием и возвратом состояния.
- Проверить email-настройки для budget/review digest, если SMTP готов.

Exit criteria:

- Operational задачи не падают.
- Результат каждой задачи виден в логах/БД/admin.
- Есть runbook для failures.

### Stage 3 — Direct-ingest cadence

Цель: убедиться, что регулярное обновление источников не повторяет
первичный bootstrap и не создает дубликаты.

Задачи:

- Для каждого direct-ingest источника выполнить dry-run/limited applied
  test:
  - `mcp_registry`;
  - `gemini_extensions`;
  - `claude_connectors`;
  - `chatgpt_apps`.
- Проверить counters: `seen`, `new`, `updated`, `skipped`, `failed`.
- Проверить, что повторный запуск не создает дубликаты.
- Для MCP Registry отдельно проверить cursor/resume поведение и что
  monorepo servers не склеиваются обратно.

Exit criteria:

- Все direct-ingest источники можно безопасно запускать по расписанию.
- Повторный запуск идемпотентен.
- LLM calls не создаются direct-ingest задачами.

### Stage 4 — Re-actualization pilot

Цель: проверить актуализацию опубликованных карточек без silent writes.

Задачи:

- Подготовить небольшой набор published карточек для pilot.
- Выполнить `reactualize_apps_batch` в dry-run.
- Выполнить limited applied run.
- Проверить, что:
  - App-поля не меняются напрямую;
  - создается `NeedsReviewQueueEntry(kind=reactualized)` только при diff;
  - `Source.last_enriched_at` обновляется;
  - пустые diffs не спамят review queue;
  - стоимость и latency записываются в `LLMCallLog`.

Exit criteria:

- Re-actualization можно включать малым daily batch.
- Есть понятные правила обработки `reactualized` proposals в admin.

### Stage 5 — Editorial automation design

Цель: решить проблему масштаба: вручную разобрать тысячи карточек
невозможно, значит нужен управляемый autopublish/autoreview слой.

Текущая система намеренно консервативна: LLM дополняет draft и создает
review proposals, но не публикует. Для полной автоматизации нужно добавить
отдельный слой принятия решений.

Предлагаемый подход:

- Ввести явные уровни доверия:
  - `trusted_direct_source`: официальный/структурированный источник;
  - `llm_enriched`: есть LLM enrichment без failures;
  - `quality_passed`: карточка проходит validation checklist;
  - `publish_candidate`: можно публиковать автоматически;
  - `needs_human_review`: нужен редактор.
- Начать с non-MCP платформ, где уже есть полный enrichment:
  - Gemini;
  - Claude;
  - ChatGPT unofficial.
- Сначала автоматизировать не публикацию, а review proposals:
  - auto-accept безопасных proposals с высоким confidence;
  - auto-reject пустые/no-op proposals;
  - оставить спорные proposals человеку.
- Затем включить pilot autopublish для малой выборки:
  - только карточки с official/install URL;
  - short/long description заполнены;
  - есть listing type и category;
  - нет link health failures;
  - нет unresolved high-risk review proposals;
  - source входит в allowlist.

Exit criteria:

- Есть формализованный publish policy.
- Есть management command/admin action для dry-run отчета:
  `would_publish`, `blocked_reason`, `estimated_count`.
- Есть limited applied pilot с rollback plan.

### Stage 6 — Non-MCP automated publication pilot

Цель: довести часть non-MCP каталога от draft до published без ручной
поштучной модерации.

Задачи:

- Реализовать/проверить quality gate для публикации.
- Прогнать dry-run report по Gemini/Claude/ChatGPT.
- Опубликовать малый batch, например 25-50 карточек.
- Проверить публичные страницы, sitemap, search, filters.
- Собрать метрики:
  - сколько published;
  - сколько blocked;
  - основные blocked reasons;
  - сколько review proposals auto-accepted/rejected;
  - сколько осталось pending.

Exit criteria:

- Есть доказанный путь `ingest -> enrich -> quality gate -> publish`.
- Понятно, какие поля/валидации мешают массовой публикации.

Checkpoint 2026-06-29:

- После первых Claude batches основной оставшийся технический блокер у
  части trusted connectors: недостаточно явных capability rows, хотя
  официальный cloud directory уже доказывает, что connector remote-hosted
  и не требует локального server setup.
- Добавлен dry-run/apply command
  `backfill_trusted_connector_capabilities`, который для trusted
  Claude/ChatGPT connector listings заполняет только отсутствующие или
  `unknown` capability rows:
  - `remote_available=yes`;
  - `local_setup_required=no`.
- Команда не перезаписывает существующие `yes/no`, исключает MCP/mixed MCP
  карточки по умолчанию и должна запускаться после direct-ingest/backfill
  перед очередным autopublish dry-run.
- Добавлен следующий guardrail для duplicate flow:
  - weak dedupe больше не считает общий platform/source directory host
    (`claude.com`, `chatgpt.com`, `mcpapp.net`, MCP Registry) доказательством
    дубликата;
  - `dismiss_directory_duplicate_candidates` умеет dry-run/apply закрывать
    уже созданные false positives только для `shared_domain_similar_name`,
    если score ниже порога настоящего name-match.
- Добавлена taxonomy category `health-wellness` и mapping Claude source
  `Health and wellness`/`Healthcare -> health-wellness`.
- Добавлен dry-run/apply command `backfill_trusted_connector_categories`,
  который добавляет только явно mapped категории из trusted connector
  source payloads и исключает MCP/mixed MCP карточки по умолчанию.

### Stage 7 — Discovery и continuous growth

Цель: проверить поиск новых приложений после bootstrap.

Задачи:

- RSS discovery dry-run/limited applied.
- GitHub MCP discovery dry-run/limited applied.
- Проверить cheap LLM classification, dedupe, cost.
- Проверить, что новые найденные кандидаты идут в enrichment/publish
  pipeline по тем же правилам, что bootstrap-карточки.

Exit criteria:

- Новые приложения добавляются инкрементально.
- Мусор отсеивается до expensive enrichment.
- Нет дубликатов и неконтролируемого роста расходов.

### Stage 8 — MCP enrichment last

Цель: обогатить MCP Registry только после того, как весь pipeline и
публикационные правила проверены на меньших источниках.

Почему последний этап:

- MCP pending enrichment: 10 880.
- Оценка стоимости: около `$28.70`, что выше текущего месячного бюджета
  `$20`.
- MCP - самый большой источник; ошибки policy/публикации на нем дадут
  максимальный шум.

Задачи:

- Поднять/утвердить отдельный budget на MCP enrichment.
- Запустить small MCP sample, например 100-200 карточек.
- Проверить quality/publish policy на MCP sample.
- Затем запускать chunks с мониторингом:
  - cost;
  - failures;
  - review queue growth;
  - auto-publish candidates;
  - blocked reasons.

Exit criteria:

- MCP enrichment завершен controlled batches.
- MCP карточки проходят тот же publish pipeline, что non-MCP.

### Stage 9 — Full autopilot guardrails

Цель: довести систему до режима, где LLM делает всю работу, но с
ограничениями, аудитом и возможностью отката.

Нужно доработать/проверить:

- лимиты на день/неделю по LLM cost и publish count;
- audit trail для каждого auto-decision;
- admin dashboard для auto-published карточек;
- rollback или unpublish action;
- мониторинг резких изменений количества карточек;
- алерты на рост failures, duplicate candidates, pending review queue;
- ручной kill switch через feature flags.

Exit criteria:

- Можно включить расписание без постоянного ручного контроля.
- Любое автоматическое изменение объяснимо и откатываемо.

## Рекомендуемый порядок ближайших работ

1. Перейти в новую LLM-сессию с prompt ниже.
2. Зафиксировать или закоммитить текущие docs changes.
3. Stage 0: проверить prod flags и отключить общий `enrich_pending`, если
   MCP enrichment откладывается, либо оставить
   `AGENT_ENRICH_PENDING_SOURCE_TYPES` без `mcp_registry`/`all`.
4. Stage 1: составить таблицу scheduled tasks из кода и production DB.
5. Stage 2-4: проверить operational tasks, direct ingest cadence и
   re-actualization.
6. Stage 5-6: спроектировать и реализовать controlled autopublish для
   non-MCP.
7. Stage 8: вернуться к MCP enrichment только после проверки pipeline.

## Prompt для новой сессии

Скопировать в новую Codex/LLM-сессию:

```text
Мы продолжаем production hardening проекта LLM App Market.

Рабочая директория:
/home/jekson/Projects/own/llmmarket

Production:
- EC2 IP: 18.194.222.102
- SSH: ssh -i /home/jekson/Downloads/wplay_key.pem ubuntu@18.194.222.102
- Project path on EC2: ~/llmapp
- Domain: https://llmappmarket.com
- DNS уже настроен, TLS работает через Caddy.

Важные правила:
- Не переносить локальную БД в production.
- Не печатать secrets из .env.
- Не запускать полный MCP enrichment без отдельного подтверждения бюджета.
- MCP enrichment оставляем на последний этап.
- Если нужно менять prod feature flags, сначала объяснить риск и проверить
  текущее non-secret состояние.
- Пользователь предпочитает проект в ~/llmapp на EC2, не /opt.
- Работать прагматично: сначала читать код/docs, потом делать изменения.

Документы, которые нужно открыть первыми:
- docs/production-readiness-ru.md
- docs/deployment-ru.md
- docs/agent-pipeline.md
- docs/project-overview-ru.md

Текущее состояние по production checkpoint от 2026-06-05:
- Production DB наполнена с нуля.
- Direct-ingest выполнен по всем источникам.
- MCP Registry: 10 915 sources, latest API сверка missing=0.
- Gemini Extensions: 1 050 sources.
- Claude Connectors: 24 sources.
- ChatGPT unofficial: 293 sources.
- Всего draft карточек: 11 952.
- Non-MCP LLM enrichment завершен:
  - Gemini 764/764 enriched.
  - Claude 24/24 enriched.
  - ChatGPT 288/288 enriched.
  - 1 035 persisted enrichment tasks, failures=0.
  - Cost non-MCP enrichment: $2.730216.
  - Total monthly LLM cost after run: $2.838061 / budget $20.
- MCP enrichment полный не запускался:
  - MCP apps: 10 915.
  - MCP pending enrichment: 10 880.
  - Estimated full MCP enrichment cost: ~$28.70.
- Review queue after non-MCP enrichment: 1 030 pending proposals.

Последняя задача:
Составлен документ docs/production-readiness-ru.md с планом "тестирование
работоспособности под ключ". Нужно продолжить с Stage 0/Stage 1:
1. Проверить git status и текущие docs changes.
2. Проверить production health and non-secret config state.
3. Убедиться, что enrich_pending не запустит MCP случайно, если MCP
   enrichment откладывается: `AGENT_ENRICH_PENDING_SOURCE_TYPES` не содержит
   `mcp_registry` или `all`.
4. Составить inventory всех scheduled tasks: расписание, flags, DB writes,
   LLM usage, cost risk, dry-run/limited-run procedure, validation queries.
5. После inventory перейти к проверке operational tasks, direct-ingest
   cadence и re-actualization pilot.

Цель пользователя:
Довести production до "под ключ": регулярное обновление каталога,
проверка актуальности, внесение изменений, review/publish flow и дальше
постепенная полная автоматизация, где LLM делает путь от поиска и
добавления до публикации. MCP enrichment выполнить в самом конце после
настройки и проверки всего pipeline.
```
