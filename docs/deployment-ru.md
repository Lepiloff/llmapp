# Деплой LLM App Market

Первичный деплой проекта + операции «дня 2» (rebuild, rollback,
мониторинг). Стек: Docker Compose на EC2 + managed Postgres +
ElastiCache Redis + домен `llmappmarket.com`.

Non-code задачи (legal, DNS, credentials, editorial-роль и т.д.)
вынесены в [`docs/pre-launch-checklist.md`](pre-launch-checklist.md) —
этот документ покрывает только техническую часть.

## Содержание

1. [Pre-requisites](#pre-requisites)
2. [Архитектура](#архитектура)
3. [Переменные окружения](#переменные-окружения)
4. [Первичный деплой](#первичный-деплой)
5. [Smoke checks](#smoke-checks)
6. [Включение background-задач](#включение-background-задач)
7. [Операции дня 2](#операции-дня-2)
8. [Rollback](#rollback)
9. [Известные особенности](#известные-особенности)

---

## Pre-requisites

| Компонент | Что нужно |
|---|---|
| EC2 | Ubuntu 22.04+, 2 vCPU / 4 GB RAM, открыты 80 + 443. Docker + docker-compose-plugin |
| Domain | `llmappmarket.com` + `www.…` указывают на EC2 IP. TLS — Cloudflare proxy (proще всего, уже используем для Turnstile) или Caddy / nginx+certbot |
| Postgres | RDS или Postgres 16 на отдельной EC2. БД `llmmarket` + пользователь. `pg_trgm` доступен для CREATE EXTENSION |
| Redis | ElastiCache или Redis 7. Открыт 6379 только из SG EC2 |
| OpenAI | API key с активным billing, доступ к `gpt-5.4-mini` + `gpt-5.4-nano` |
| GitHub | Fine-grained PAT, read-only public-repo scope (30 req/min на Search + 5000/h на Contents) |
| SMTP | SES / SendGrid / Postmark для review-digest и budget alerts |
| S3-совместимый bucket | Для off-host backup'ов (`pg_backup` сервис) |

---

## Архитектура

`docker-compose.yml` поднимает 5 сервисов + 1 опциональный backup-runner:

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
      │           │   beat     │  django_celery_beat, ~19 cron-задач
      │           │ (celery)   │  (discovery, retention, sitemap, …)
      │           └────────────┘
      │
┌─────┴───────┐
│  pg_backup  │  postgres:16 + awscli, profile=production
│  (runner)   │  → pg_dump в named volume + опц. S3 upload
└─────────────┘
```

* **web** — gunicorn в контейнере. WhiteNoise отдаёт `/static/*` напрямую
  с cache-busting hashes, nginx-фронт необязателен.
* **worker** — Celery, исполняет LLM-pipeline, link checks, re-actualization,
  search-vector refresh, sitemap rebuild, retention cleanup.
* **beat** — Celery beat scheduler.
* **pg_backup** — отдельный prod-only сервис. Картинка
  `docker/Dockerfile.pg_backup` (pg_dump + awscli baked in, без apt-install
  на старте). Бэкап-цикл: dump → gzip → опц. S3 upload → trim
  retention → sleep.
* **postgres / redis** — в проде заменяются на managed-сервисы через
  `DATABASE_URL` / `REDIS_URL`.

На EC2 typically запущены: `web`, `worker`, `beat`, `pg_backup` (через
`--profile production`).

---

## Переменные окружения

**Полный список с комментариями — в [`.env.example`](../.env.example) и
[`.env.production`](../.env.production).** Здесь — только ключевые
callout'ы.

### 🔴 Required — без них boot падает или работа небезопасна

* `SECRET_KEY` — высокоэнтропийный (`python -c 'import secrets; print(secrets.token_urlsafe(64))'`). **Прод-settings hard-fail'ят с пустым или дефолтным insecure значением.**
* `DJANGO_SETTINGS_MODULE=config.settings.prod`
* `ALLOWED_HOSTS=llmappmarket.com,www.llmappmarket.com`
* `CSRF_TRUSTED_ORIGINS=https://llmappmarket.com,https://www.llmappmarket.com`
* `SITE_BASE_URL=https://llmappmarket.com`
* `DATABASE_URL` + `REDIS_URL` + `CELERY_BROKER_URL`
* `OPENAI_API_KEY` + `AGENT_LLM_MODEL_PRIMARY` + `AGENT_LLM_MODEL_CHEAP`
* 6 `AGENT_OPENAI_*_COST_PER_1M_TOKENS` ключей (используются для cost
  attribution и budget hard-stop)
* `AGENT_MONTHLY_BUDGET_USD` — обязательно. Прод бутится с budget=0 →
  hard-stop сработает сразу.

### 🟡 Strongly recommended

* `GITHUB_TOKEN` — для discovery, без него Search API упрётся на 10 req/min.
* `SENTRY_DSN` — без него прод-инциденты не видны.
* `TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET_KEY` — иначе `/submit/` обходится скриптами.
* `EMAIL_HOST` + `EMAIL_HOST_USER` + `EMAIL_HOST_PASSWORD` — для review-digest и budget alerts.
* `AGENT_REVIEW_DIGEST_EMAILS` / `AGENT_BUDGET_ALERT_EMAILS` / `SUBMISSIONS_NOTIFY_EMAILS`.

### 🟢 Feature flags — выключены по умолчанию

* `AGENT_SOURCES_ENABLED=` — discovery off. Включить: `github_mcp,rss`.
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

---

## Первичный деплой

```bash
# 1. Подготовить EC2
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker ubuntu                 # relogin

# 2. Клонировать и настроить env
cd /opt && sudo git clone https://github.com/<you>/llmmarket.git
sudo chown -R ubuntu:ubuntu llmmarket && cd llmmarket
cp .env.production .env
nano .env                                       # заполнить по списку выше

# 3. Собрать и поднять
docker compose --profile production up -d --build
```

Первая сборка 3-5 минут. Entrypoint автоматически: ждёт Postgres+Redis →
migrate → seed reference data (платформы / категории / capabilities /
listing-types) → опц. createsuperuser → запускает процесс.

### nginx / TLS

WhiteNoise отдаёт `/static/*` сам, поэтому nginx нужен **только если
ты не используешь Cloudflare proxy**. Минимальный конфиг:

```nginx
server {
    listen 443 ssl http2;
    server_name llmappmarket.com www.llmappmarket.com;
    ssl_certificate     /etc/letsencrypt/.../fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/.../privkey.pem;
    client_max_body_size 10m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
server { listen 80; server_name llmappmarket.com www.…; return 301 https://$host$request_uri; }
```

С Cloudflare proxy всё проще: TLS на их стороне, gunicorn слышит HTTP,
нужно только настроить origin-pull rule.

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

Ожидаемо ~19 enabled задач: ingest, discovery×2, re-actualize, link
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

## Включение background-задач

По умолчанию discovery + re-actualization выключены — намеренно. Первый
запуск делается dry-run'ом, не «по cron'у в 6:30 утра».

### Discovery

```bash
# Dry-run, ~$0.01-0.02
docker compose exec web python manage.py agent_run --source=github_mcp --limit=5

# Результат осмысленный → включить
echo 'AGENT_SOURCES_ENABLED=github_mcp,rss' >> .env
docker compose restart worker beat
```

Beat: `discover_github_mcp` Пн/Ср/Пт 06:30 UTC, `discover_rss` каждые 6 ч.

### Re-actualization

```bash
docker compose exec web python manage.py shell -c \
  "from apps.agent.tasks import reactualize_apps_batch; print(reactualize_apps_batch(limit=3, dry_run=True))"

echo 'AGENT_REACTUALIZATION_ENABLED=True' >> .env
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
cd /opt/llmmarket && git pull
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
cd /opt/llmmarket
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

### Static — WhiteNoise vs nginx
Sprint 2 добавил WhiteNoise — gunicorn сам отдаёт `/static/*` с
правильными cache-busting хэшами. nginx-блок в этом документе — на
случай если используется без Cloudflare proxy для TLS. Если уже
есть Cloudflare → nginx не нужен.

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
