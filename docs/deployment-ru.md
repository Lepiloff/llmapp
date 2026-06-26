# Деплой LLM App Market

Первичный деплой проекта + операции «дня 2» (rebuild, rollback,
мониторинг). Базовый стек для первого запуска: один EC2, Docker Compose,
локальные Postgres 16 + Redis 7, Caddy с автоматическим TLS и домен
`llmappmarket.com`. После запуска Postgres и Redis можно вынести в RDS /
ElastiCache без изменения приложения.

Non-code задачи (legal, DNS, credentials, editorial-роль и т.д.)
вынесены в [`docs/pre-launch-checklist.md`](pre-launch-checklist.md) —
этот документ покрывает только техническую часть.

## Содержание

1. [Pre-requisites](#pre-requisites)
2. [Архитектура](#архитектура)
3. [Переменные окружения](#переменные-окружения)
4. [Первичный деплой](#первичный-деплой)
5. [Smoke checks](#smoke-checks)
6. [Первичное наполнение пустой БД](#первичное-наполнение-пустой-бд)
7. [Включение background-задач](#включение-background-задач)
8. [Операции дня 2](#операции-дня-2)
9. [Rollback](#rollback)
10. [Известные особенности](#известные-особенности)

---

## Pre-requisites

| Компонент | Что нужно |
|---|---|
| EC2 | Ubuntu 22.04+, 2 vCPU / 4 GB RAM, Elastic IP. В Security Group открыты `22/tcp` только с твоего IP и `80/tcp`, `443/tcp`, `443/udp` для всех |
| Domain | A-records `llmappmarket.com` + `www.llmappmarket.com` указывают на Elastic IP. На первом запуске использовать DNS-only, TLS выдаст Caddy |
| Postgres | На первом запуске контейнер Postgres 16 на EC2. Внешний порт привязан только к `127.0.0.1`; `pg_trgm` и `unaccent` создаёт entrypoint |
| Redis | На первом запуске контейнер Redis 7 без публичного порта |
| OpenAI | API key с активным billing, доступ к `gpt-5.4-mini` + `gpt-5.4-nano` |
| GitHub | Fine-grained PAT, read-only public-repo scope (30 req/min на Search + 5000/h на Contents) |
| SMTP | SES / SendGrid / Postmark для review-digest и budget alerts |
| S3-совместимый bucket | Для off-host backup'ов (`pg_backup` сервис) |

---

## Архитектура

`docker-compose.yml` поднимает 5 базовых сервисов + production edge и
backup-runner:

```
                   ┌────────────┐
                   │   web      │  gunicorn + WhiteNoise (static)
                   │  (Django)  │  /health/, /api/v1/*, /admin/, страницы
                   └─────┬──────┘
                         │ async tasks
                         ▼
┌──────────┐      ┌────────────┐      ┌──────────────┐
│ postgres │ ◄──► │   worker   │ ◄──► │    redis     │
│   (16)   │      │  (celery)  │      │ (broker+rate │
└──────────┘      └────────────┘      │  limit+cache)│
      ▲                  ▲             └──────────────┘
      │                  │ schedules
      │           ┌──────┴─────┐
      │           │   beat     │  django_celery_beat, ~20 cron-задач
      │           │ (celery)   │  (discovery, retention, sitemap, …)
      │           └────────────┘
      │
┌─────┴───────┐
│  pg_backup  │  postgres:16 + awscli, profile=production
│  (runner)   │  → pg_dump в named volume + опц. S3 upload
└─────────────┘

┌─────────────┐
│    caddy    │  profile=production, ports 80/443
│ TLS + proxy │  → automatic Let's Encrypt → web:8000
└─────────────┘
```

* **web** — gunicorn в контейнере. WhiteNoise отдаёт `/static/*` напрямую
  с cache-busting hashes.
* **worker** — Celery, исполняет LLM-pipeline, link checks, re-actualization,
  search-vector refresh, sitemap rebuild, retention cleanup.
* **beat** — Celery beat scheduler.
* **pg_backup** — отдельный prod-only сервис. Картинка
  `docker/Dockerfile.pg_backup` (pg_dump + awscli baked in, без apt-install
  на старте). Бэкап-цикл: dump → gzip → опц. S3 upload → trim
  retention → sleep.
* **caddy** — prod-only edge proxy с автоматическим Let's Encrypt TLS.
* **postgres / redis** — в базовом деплое работают на том же EC2 в
  контейнерах. Для следующего этапа их можно заменить managed-сервисами
  через `DATABASE_URL` / `REDIS_URL`.

На EC2 запущены: `postgres`, `redis`, `web`, `worker`, `beat`, `caddy`,
`pg_backup` (последние два через `--profile production`).

---

## Переменные окружения

**Полный список с комментариями — в [`.env.example`](../.env.example) и
[`.env.production.example`](../.env.production.example).** Здесь — только ключевые
callout'ы.

### 🔴 Required — без них boot падает или работа небезопасна

* `SECRET_KEY` — высокоэнтропийный (`python -c 'import secrets; print(secrets.token_urlsafe(64))'`). **Прод-settings hard-fail'ят с пустым или дефолтным insecure значением.**
* `DJANGO_SETTINGS_MODULE=config.settings.prod`
* `ALLOWED_HOSTS=llmappmarket.com,www.llmappmarket.com`
* `CSRF_TRUSTED_ORIGINS=https://llmappmarket.com,https://www.llmappmarket.com`
* `SITE_BASE_URL=https://llmappmarket.com`
* `POSTGRES_PASSWORD` + `DATABASE_URL` + `REDIS_URL` + `CELERY_BROKER_URL`

### 🔴 Required перед включением LLM-задач

* `OPENAI_API_KEY` + `AGENT_LLM_MODEL_PRIMARY` + `AGENT_LLM_MODEL_CHEAP`.
* 6 `AGENT_OPENAI_*_COST_PER_1M_TOKENS` ключей (используются для cost
  attribution и budget hard-stop).
* `AGENT_MONTHLY_BUDGET_USD` — обязательно. Пустое значение или `0`
  отключает budget enforcement и недопустимо при реальных API-вызовах.

### 🟡 Strongly recommended

* `GITHUB_TOKEN` — для discovery, без него Search API упрётся на 10 req/min.
* `SENTRY_DSN` — без него прод-инциденты не видны.
* `TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET_KEY` — иначе `/submit/` обходится скриптами.
* `EMAIL_HOST` + `EMAIL_HOST_USER` + `EMAIL_HOST_PASSWORD` — для review-digest и budget alerts.
* `AGENT_REVIEW_DIGEST_EMAILS` / `AGENT_BUDGET_ALERT_EMAILS` / `SUBMISSIONS_NOTIFY_EMAILS`.
* `MCP_REGISTRY_TIMEOUT_SECONDS=90` и `AGENT_OPENAI_TIMEOUT_SECONDS=90` —
  дефолты уже такие, но значения стоит держать явно в продовом `.env`.

### 🟢 Feature flags — выключены по умолчанию

* `AGENT_SOURCES_ENABLED=` — discovery off. Полный список:
  `gemini_extensions,claude_connectors,chatgpt_apps,github_mcp,rss,enrich_pending`.
* `AGENT_REACTUALIZATION_ENABLED=False`.
* `AGENT_RATE_LIMIT_RPS_PER_DOMAIN=1.0` — enforced cross-process через Redis.

### 🟣 Backup (production profile)

* `PG_BACKUP_S3_BUCKET=` — пусто = только локальный named-volume.
* `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_DEFAULT_REGION` — IAM с write-only на bucket.
* `PG_BACKUP_RETENTION_DAYS=14`, `PG_BACKUP_INTERVAL_SECONDS=86400`.

### Bootstrap superuser (первый запуск)

Все три задать → entrypoint создаёт суперюзера если ещё нет:
```
DJANGO_SUPERUSER_USERNAME / _EMAIL / _PASSWORD
```
После первого boot переменные можно убрать.

Для односерверного Postgres сгенерировать отдельный пароль и указать
одно и то же значение в `POSTGRES_PASSWORD` и внутри `DATABASE_URL`:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

---

## Первичный деплой

```bash
# 1. На EC2 установить git, Docker Engine и compose-plugin.
# Использовать официальный Docker apt repository:
# https://docs.docker.com/engine/install/ubuntu/
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu
# Перезайти по SSH, затем проверить:
docker compose version

# 2. Клонировать и настроить env
cd ~
git clone https://github.com/Lepiloff/llmapp.git llmapp
cd llmapp
cp .env.production.example .env
chmod 600 .env
nano .env

# 3. Убедиться, что DNS A-records уже указывают на Elastic IP.
getent ahostsv4 llmappmarket.com
getent ahostsv4 www.llmappmarket.com

# 4. Собрать и поднять
docker compose --profile production up -d --build
```

Первая сборка 3-5 минут. Entrypoint автоматически: ждёт Postgres+Redis →
migrate → seed reference data (платформы / категории / capabilities /
listing-types) → опц. createsuperuser → запускает процесс.

### Caddy / TLS

Production profile поднимает `caddy:2.11-alpine`. Конфиг в
[`docker/caddy/Caddyfile`](../docker/caddy/Caddyfile) проксирует запросы
на `web:8000`. Когда A-records указывают на EC2 и порты `80`, `443`
доступны извне, Caddy сам получает и продлевает Let's Encrypt
сертификаты, а HTTP перенаправляет на HTTPS.

```bash
docker compose --profile production logs caddy --tail=100
curl -fsS https://llmappmarket.com/health/
```

Cloudflare proxy для первого запуска не нужен. Turnstile работает
независимо от проксирования трафика. Если позже включаешь Cloudflare
proxy, используй режим SSL/TLS `Full (strict)`: origin уже обслуживается
Caddy по HTTPS.

---

## Smoke checks

После старта прогнать (5 минут):

```bash
# Health (4 чека: DB, Redis, pg_trgm, celery_worker)
curl -fsS https://llmappmarket.com/health/
# → {"status":"ok","checks":{"db":true,"redis":true,"pg_trgm":true,"celery_worker":true}}

# Public страницы
curl -fsS https://llmappmarket.com/ | grep -q "Trending now" && echo OK
curl -fsS https://llmappmarket.com/apps/ | head -c 200
curl -fsS https://llmappmarket.com/sitemap.xml | head -3
curl -fsS https://llmappmarket.com/robots.txt
curl -sw '%{http_code}\n' -o /dev/null https://llmappmarket.com/apps/no-such/  # → 404

# Public API (Sprint 3)
curl -fsS https://llmappmarket.com/api/v1/apps/?page_size=2
curl -fsS https://llmappmarket.com/api/v1/platforms/
curl -sI  https://llmappmarket.com/api/v1/docs              # OpenAPI Swagger

# CSP headers (Sprint 3) — должен быть Content-Security-Policy header
curl -sI https://llmappmarket.com/ | grep -i content-security
```

### Admin

1. `/admin/` → залогиниться суперпользователем.
2. Доступны: AgentRun, BudgetMonthState, NeedsReviewQueueEntry, App,
   Source, LinkHealth, TrendingScore.
3. `/admin/agent/agentrun/cost-dashboard/` — dashboard рендерится.
4. `/admin/agent/needsreviewqueueentry/sla-dashboard/` — SLA-snapshot
   (Sprint 2) — должен показать «OK» при пустой очереди.

### Beat schedule

```bash
docker compose exec web python manage.py shell -c "
from django_celery_beat.models import PeriodicTask
for t in PeriodicTask.objects.order_by('name'):
    print(t.name, t.enabled)
"
```

Ожидаемо ~20 enabled задач: ingest, discovery×2, pending enrichment,
re-actualize, link
checker, budget check, review digest, sitemap rebuild, sitemap ping,
trending scores, search vectors, popular searches, SEO reports,
quality recalc, newsletter draft, retention × 4 (agent/links/searches/analytics).

### pg_backup (production profile)

```bash
docker compose --profile production logs pg_backup --tail=20
# → "📦 dumping → /backups/llmmarket-…sql.gz"
# → "✅ dump complete (NNN K)"
# → если PG_BACKUP_S3_BUCKET задан: "✅ s3 upload complete"
```

---

## Первичное наполнение пустой БД

Локальную dev-БД в production не переносим. После первого успешного
smoke-check запускаем bootstrap прямо на EC2:

```bash
cd ~/llmapp
./scripts/bootstrap_prod_catalog.sh
```

Скрипт запускает MCP Registry v0, Gemini Extensions, Claude Connectors
и ChatGPT Apps direct-ingest. По умолчанию direct-ingest берёт до 500
записей на источник; для другого объёма:

```bash
DIRECT_INGEST_LIMIT=1000 ./scripts/bootstrap_prod_catalog.sh
GEMINI_EXTENSIONS_LIMIT=250 CLAUDE_CONNECTORS_LIMIT=100 CHATGPT_APPS_LIMIT=100 ./scripts/bootstrap_prod_catalog.sh
```

Direct-ingest создаёт `DRAFT`-карточки без автопубликации и без
OpenAI-вызовов. Это базовый слой каталога; после настройки LLM-кредов
запускается отдельный `enrich_pending` проход по этим DRAFT-карточкам.
Результат проверить в:

```text
/admin/catalog/app/?status__exact=draft
/admin/sources/duplicatecandidate/
```

После ручной проверки редактор отмечает карточки reviewed и публикует
admin action'ом `Publish selected (with validation)`. Это намеренный
quality gate: фоновые задачи добавляют и обновляют кандидатов
автоматически, но не публикуют непроверенный контент.

### Production checkpoint — 2026-06-05

Состояние production после первичного bootstrap и ручных applied runs:

- Локальная dev-БД не переносилась; production база была наполнена с нуля.
- Direct-ingest выполнен по всем текущим платформам/источникам:
  - `mcp_registry`: 10 915 sources. Сверено с MCP Registry API:
    `latest=10 915`, `missing=0`, `extra_not_latest=0`.
  - `gemini_extensions`: 1 050 sources.
  - `claude_connectors`: 24 sources.
  - `chatgpt_unofficial`: 293 sources.
- Каталог содержит 11 952 `draft` карточки. Автопубликации не было.
- LLM enrichment выполнен только для non-MCP платформ:
  - Gemini: 764/764 apps enriched, pending=0.
  - Claude: 24/24 apps enriched, pending=0.
  - ChatGPT: 288/288 apps enriched, pending=0.
  - Новых non-MCP enrichment tasks: 1 035 persisted, failures=0.
  - Non-MCP enrichment cost: `$2.730216`; total monthly LLM cost после
    прогона: `$2.838061` при бюджете `$20`.
- MCP Registry enrichment намеренно не выполнялся как полный прогон:
  - MCP apps: 10 915.
  - Уже enriched: 35 (ранние pilot/beat/manual runs).
  - Pending MCP enrichment: 10 880.
  - Оценка полного MCP enrichment по фактической non-MCP средней цене:
    около `$28.70`.
- Review queue после non-MCP enrichment: 1 030 pending proposals. Это не
  означает публикацию: LLM заполнил безопасные draft-поля и создал
  предложения для редактора, но `App.status` остался `draft`.

Следующая рабочая точка:

1. Решить, запускать ли полный MCP enrichment отдельным budget-approved
   batch.
2. Если MCP enrichment пока не запускаем, убрать `enrich_pending` из
   `AGENT_SOURCES_ENABLED` или заменить общий beat на source-scoped ручные
   batches: общий selector включает MCP и начнёт тратить бюджет на MCP
   после того как non-MCP pending=0.
3. После решения по MCP переходить к проверке scheduled scripts:
   direct-ingest cadence, budget alerts, re-actualization dry-run/limited
   applied run.

---

## Включение background-задач

MCP Registry ingest включён всегда и запускается ежедневно в `04:00 UTC`.
Остальные источники и re-actualization выключены feature flags'ами.

Если MCP Registry оборвался на upstream timeout, продолжить с cursor из
лога можно вручную:

```bash
docker compose exec -T web python manage.py agent_run \
  --source=mcp_registry --apply \
  --mcp-start-cursor='io.github.example/server:1.0.0' \
  --mcp-timeout=180
```

Если старый импорт склеил несколько MCP Registry servers из одного
monorepo в одну карточку, сначала проверить и затем применить repair:

```bash
docker compose exec -T web python manage.py repair_mcp_registry_splits --dry-run
docker compose exec -T web python manage.py repair_mcp_registry_splits
```

### Direct-ingest источники

После проверки bootstrap-карточек включить регулярное обновление
источников без LLM-затрат:

```bash
nano .env
# AGENT_SOURCES_ENABLED=gemini_extensions,claude_connectors,chatgpt_apps
docker compose restart worker beat
```

Beat: Gemini ежедневно `04:30 UTC`, Claude по вторникам `04:45 UTC`,
ChatGPT Apps по средам `04:45 UTC`.

### LLM discovery + enrichment

```bash
# Dry-run, ~$0.01-0.02
docker compose exec web python manage.py agent_run --source=github_mcp --limit=5

# Результат осмысленный → добавить флаги в существующую строку .env:
# AGENT_SOURCES_ENABLED=gemini_extensions,claude_connectors,chatgpt_apps,github_mcp,rss,enrich_pending
docker compose restart worker beat
```

`github_mcp` и `rss` классифицируют кандидатов дешёвой моделью и
обогащают релевантные карточки основной моделью. Direct-ingest источники
(`mcp_registry`, `gemini_extensions`, `claude_connectors`, `chatgpt_apps`)
создают только базовые DRAFT-карточки из доверенных каталогов; после
добавления LLM-кредов их обрабатывает отдельный `enrich_pending` проход.
Повторно импортировать локальную БД или пересоздавать карточки для этого
не нужно: enrichment идёт по уже созданным DRAFT-записям. Beat: GitHub MCP
Пн/Ср/Пт `06:30 UTC`, pending enrichment ежедневно `06:45 UTC`, RSS каждые
6 ч.

### Re-actualization

```bash
docker compose exec web python manage.py shell -c \
  "from apps.agent.tasks import reactualize_apps_batch; print(reactualize_apps_batch(limit=3, dry_run=True))"

nano .env  # AGENT_REACTUALIZATION_ENABLED=True
docker compose restart worker beat
```

Beat: `reactualize_apps_batch` ежедневно 07:00 UTC.

### Sanity-проверка budget alert

Один раз форсировать чтобы убедиться что email уходит:

```bash
# Поднять low-budget на минуту
AGENT_MONTHLY_BUDGET_USD=0.10 docker compose restart worker
docker compose exec web python manage.py shell -c \
  "from apps.agent.tasks import agent_budget_check; print(agent_budget_check())"
# Должно прислать письмо «100% reached». Вернуть budget и сбросить
# latches в /admin/agent/budgetmonthstate/.
```

---

## Операции дня 2

### Логи
```bash
docker compose logs -f web --tail=100
docker compose logs -f worker --tail=100
docker compose logs -f beat --tail=100
docker compose --profile production logs -f caddy --tail=100
docker compose --profile production logs -f pg_backup --tail=20
```

### Cost / budget
* `/admin/agent/agentrun/cost-dashboard/` — текущий month spend, per-day, per-model, top-10 expensive runs.
* `/admin/agent/budgetmonthstate/` — latches. Сбросить вручную = очистить таймштамп + save.

### Editorial queue
* `/admin/agent/needsreviewqueueentry/` — что LLM предложил, но не применил.
  Kind'ы: `enriched`, `reactualized`, `vanished`. Действия inline:
  Apply verdict / Approve & publish / Reject / Mark resolved.
* `/admin/agent/needsreviewqueueentry/sla-dashboard/` — overdue snapshot.

### UseCase dedup
Когда synonyms накапливаются (`/admin/catalog/usecase/` отсортировать по
app_count) — admin action «Merge selected use-cases into one canonical
row».

### Apply изменений кода
```bash
cd ~/llmapp && git pull
docker compose --profile production up -d --build
# Миграции прогонит entrypoint.
```

### Restore из backup
```bash
docker compose --profile production exec pg_backup ls /backups/
docker compose --profile production exec pg_backup \
  gunzip -c /backups/llmmarket-…sql.gz | \
  docker compose exec -T postgres psql -U llmmarket llmmarket
```

---

## Rollback

Миграции Sprint 1-3 аддитивные (новые таблицы / поля / индексы), кроме
`catalog.0002` (CharField 200→500 — обратно-совместимый расширение).
Откат = переключить код:

```bash
cd ~/llmapp
git log --oneline -10
git checkout <good-commit-sha>
docker compose --profile production up -d --build
```

Если future-миграция добавит NOT NULL / drop column — reverse её сначала:
```bash
docker compose exec web python manage.py migrate <app> <previous-name>
```

Последняя линия защиты — `pg_backup` дампы. Не отказываться от
automated S3 upload.

---

## Известные особенности

### MCP Registry 404
`https://registry.modelcontextprotocol.io/v1/servers` лежит с
2026-05-15. Sprint 2 добавил Sentry-counter c stable fingerprint —
все 404 коалесцируются в одну Sentry-issue. Sprint 3 GitHub-MCP
discovery — основной активный источник. **Action item:** периодически
смотреть Sentry на эту issue + проверять новый URL.

### F4 broker auto-fallback
`manage.py agent_run` из host-venv (вне контейнера) не резолвит имя
`redis` → автоматически переключается в `CELERY_TASK_ALWAYS_EAGER=True`
+ stderr warning. В контейнере (где Redis доступен) fallback не
срабатывает.

### Phase 4 reactualize: use_case noise
LLM каждый прогон формулирует use-case titles чуть иначе. Diff
не зажигает queue entry на use-case-only drift — иначе редактор
получал бы spam на каждой re-actualization. Если drift'ает что-то
ещё — use_case delta всё равно входит в payload и видна в админке.

### Cached input cost
OpenAI бьёт cached input ~10% от standard. `_estimate_cost_usd`
учитывает это; при изменении ratio со стороны OpenAI обновить
`AGENT_OPENAI_*_CACHED_COST_PER_1M_TOKENS` + прогнать
`agent_backfill_costs --include-nonzero` для re-pricing исторических
строк.

### Static — WhiteNoise + Caddy
Gunicorn отдаёт `/static/*` через WhiteNoise с правильными
cache-busting хэшами. Caddy завершает TLS и проксирует запросы к
gunicorn; отдельный nginx не нужен.

### CSP в браузере
Sprint 3 включил Content-Security-Policy в prod-настройках. Allowlist
покрывает Cloudflare Turnstile + Tailwind CDN + unpkg (htmx/Alpine) +
Google Fonts. Если добавляешь новый CDN-asset — пропиши в
`apps/core/csp.py` иначе браузер заблокирует. Inline-`<script>` —
только с `{{ request.csp_nonce }}`.

### og-default.png placeholder
Sprint 3 smoke-test сгенерировал placeholder PNG, чтобы
`ManifestStaticFilesStorage` не падал на отсутствующем файле. Для
launch'а — заменить на дизайнерский asset (см.
`docs/pre-launch-checklist.md` § 0.6).
