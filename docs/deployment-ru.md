# Деплой LLM App Market на EC2

Документ описывает первичный деплой проекта на свежий EC2-инстанс
плюс операции "дня 2" (rebuild, rollback, мониторинг). Целевой стек:
Docker Compose на EC2 + managed Postgres + ElastiCache Redis +
домен `llmappmarket.com`.

## Содержание

1. [Pre-requisites](#pre-requisites)
2. [Архитектура deploy](#архитектура-deploy)
3. [Переменные окружения](#переменные-окружения)
4. [Первичный деплой](#первичный-деплой)
5. [Post-deploy smoke checks](#post-deploy-smoke-checks)
6. [Включение background-задач](#включение-background-задач)
7. [Операции дня 2](#операции-дня-2)
8. [Rollback](#rollback)
9. [Известные особенности](#известные-особенности)

---

## Pre-requisites

Должно быть готово до деплоя:

| Компонент | Что нужно |
|---|---|
| EC2 инстанс | Ubuntu 22.04+, минимум 2 vCPU / 4 GB RAM, открыты порты 80 + 443. Docker + docker-compose-plugin установлены |
| Domain | `llmappmarket.com` + `www.llmappmarket.com` указывают A-records на EC2 IP. SSL через Let's Encrypt (nginx + certbot) или ALB+ACM |
| Postgres | Managed (RDS) или Postgres 16 на отдельной EC2. Создана БД `llmmarket` + пользователь с полными правами. Расширение `pg_trgm` доступно для CREATE EXTENSION (rds.force_ssl=1 не блокирует, но проверь параметрами) |
| Redis | ElastiCache или Redis 7 на отдельном инстансе. Открыт TCP 6379 только из security group EC2 |
| OpenAI API | API key с активным billing, доступ к моделям `gpt-5.4-mini` (primary) и `gpt-5.4-nano` (cheap) |
| GitHub token | Fine-grained PAT с read-only public-repo scope. 30 req/min на Search API и 5000 req/h на Contents (без токена — 10/мин, упрётесь на первом же discovery batch) |
| SMTP | Любой transactional-провайдер (SES / SendGrid / Mailgun) для review queue digest и budget alerts |

---

## Архитектура deploy

`docker-compose.yml` поднимает 5 сервисов:

```
                   ┌────────────┐
                   │   web      │  gunicorn, обрабатывает HTTP
                   │  (Django)  │  /health/, публичные страницы, admin
                   └─────┬──────┘
                         │ async tasks
                         ▼
┌──────────┐      ┌────────────┐      ┌──────────────┐
│ postgres │ ◄──► │   worker   │ ◄──► │    redis     │
│   (16)   │      │  (celery)  │      │ (broker+cache)│
└──────────┘      └────────────┘      └──────────────┘
                         ▲
                         │ schedules
                  ┌──────┴─────┐
                  │   beat     │  django_celery_beat, DatabaseScheduler
                  │ (celery)   │  раз в минуту проверяет расписание
                  └────────────┘
```

* **web** — gunicorn внутри контейнера; принимает HTTPS-трафик от nginx/ALB
* **worker** — Celery, исполняет агентный pipeline (LLM-вызовы, link checks,
  re-actualization, search vector refresh, sitemap rebuild)
* **beat** — Celery beat scheduler; держит расписание 10 cron-задач в БД
* **postgres** + **redis** — в проде заменяются на managed-сервисы, в
  docker-compose оставлены только для локальной разработки

На прод EC2 typically остаются `web`, `worker`, `beat`; `postgres` и
`redis` указываются через `DATABASE_URL` / `CELERY_BROKER_URL`.

---

## Переменные окружения

Полный список — в `.env.example`. Ниже разбит по приоритету.

### 🔴 Required (без них контейнер не стартует или работает небезопасно)

```bash
# Базовое
SECRET_KEY=<86+ chars random>          # python -c 'import secrets; print(secrets.token_urlsafe(64))'
DEBUG=False                            # неявно через config/settings/prod.py
DJANGO_SETTINGS_MODULE=config.settings.prod
ALLOWED_HOSTS=llmappmarket.com,www.llmappmarket.com
CSRF_TRUSTED_ORIGINS=https://llmappmarket.com,https://www.llmappmarket.com
SITE_BASE_URL=https://llmappmarket.com   # используется в admin-emails для абсолютных URL

# БД и broker
DATABASE_URL=postgres://user:pass@rds-endpoint:5432/llmmarket
CELERY_BROKER_URL=redis://elasticache-endpoint:6379/0
REDIS_URL=redis://elasticache-endpoint:6379/0

# LLM
AGENT_LLM_PROVIDER_PRIMARY=openai
AGENT_LLM_MODEL_PRIMARY=gpt-5.4-mini
AGENT_LLM_PROVIDER_CHEAP=openai
AGENT_LLM_MODEL_CHEAP=gpt-5.4-nano
OPENAI_API_KEY=sk-proj-...

# Цены LLM (используются для расчёта стоимости каждого вызова + бюджетных латчей)
AGENT_OPENAI_PRIMARY_INPUT_COST_PER_1M_TOKENS=0.75
AGENT_OPENAI_PRIMARY_CACHED_COST_PER_1M_TOKENS=0.075
AGENT_OPENAI_PRIMARY_OUTPUT_COST_PER_1M_TOKENS=4.50
AGENT_OPENAI_CHEAP_INPUT_COST_PER_1M_TOKENS=0.20
AGENT_OPENAI_CHEAP_CACHED_COST_PER_1M_TOKENS=0.02
AGENT_OPENAI_CHEAP_OUTPUT_COST_PER_1M_TOKENS=1.25

# Бюджет
AGENT_MONTHLY_BUDGET_USD=20
```

### 🟡 Strongly recommended

```bash
# Discovery / GitHub
GITHUB_TOKEN=github_pat_...

# Алерты по бюджету (80% / 100%)
AGENT_BUDGET_ALERT_EMAILS=ops@llmappmarket.com

# Editorial / review queue digest (07:30 UTC ежедневно)
AGENT_REVIEW_DIGEST_EMAILS=editorial@llmappmarket.com
SUBMISSIONS_NOTIFY_EMAILS=editorial@llmappmarket.com

# CORS (если будет фронтенд на отдельном поддомене)
CORS_ALLOWED_ORIGINS=https://app.llmappmarket.com

# Email transport
EMAIL_HOST=email-smtp.eu-west-1.amazonaws.com
EMAIL_HOST_USER=AKIA...
EMAIL_HOST_PASSWORD=...
EMAIL_PORT=587
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=noreply@llmappmarket.com

# Sentry
SENTRY_DSN=https://...@sentry.io/...

# Cloudflare Turnstile (если включаешь капчу на /submit/)
TURNSTILE_SITE_KEY=...
TURNSTILE_SECRET_KEY=...
```

### 🟢 Optional / feature flags (выключены по умолчанию)

```bash
# Phase 4 re-actualization beat
AGENT_REACTUALIZATION_ENABLED=False              # → True когда готов запускать
AGENT_REACTUALIZATION_INTERVAL_DAYS=30
AGENT_REACTUALIZATION_BATCH_SIZE=20

# Phase 3 discovery sources
AGENT_SOURCES_ENABLED=                           # пусто = вся discovery выключена
# Включить: AGENT_SOURCES_ENABLED=github_mcp,rss

# Прочее
AGENT_RATE_LIMIT_RPS_PER_DOMAIN=1.0
CELERY_TASK_ALWAYS_EAGER=False
MCP_REGISTRY_BASE_URL=https://registry.modelcontextprotocol.io/v1
```

### Bootstrap superuser (опционально, только на первом запуске)

```bash
DJANGO_SUPERUSER_USERNAME=admin
DJANGO_SUPERUSER_EMAIL=admin@llmappmarket.com
DJANGO_SUPERUSER_PASSWORD=<strong-password>
```

При наличии всех трёх — entrypoint создаст суперпользователя при первом
старте если ещё нет. На второй запуск переменные можно убрать.

---

## Первичный деплой

### Шаг 1. Подготовка инстанса

```bash
# На EC2
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker ubuntu  # разлогиниться/залогиниться
```

### Шаг 2. Клонирование репозитория

```bash
cd /opt
sudo git clone https://github.com/<you>/llmmarket.git
sudo chown -R ubuntu:ubuntu llmmarket
cd llmmarket
```

### Шаг 3. Создание `.env`

```bash
cp .env.example .env
# отредактировать .env по списку Required выше:
nano .env
```

### Шаг 4. Сборка образов

```bash
docker compose build web worker beat
```

Занимает 3-5 минут на первой сборке.

### Шаг 5. Запуск контейнеров

```bash
docker compose up -d web worker beat
```

При первом старте entrypoint:
1. Дожидается готовности Postgres и Redis
2. Применяет миграции (`migrate --noinput`)
3. Сидит reference-данные (`seed_demo` — платформы / категории / capabilities / listing-types)
4. Если переменные `DJANGO_SUPERUSER_*` заданы — создаёт суперюзера
5. Запускает соответствующий процесс (gunicorn / celery worker / celery beat)

### Шаг 6. Verify health

```bash
docker compose ps                                # все healthy
docker compose exec web curl http://localhost:8000/health/
# ожидаем: {"status":"ok","checks":{"db":true,"redis":true,"pg_trgm":true}}
```

### Шаг 7. nginx + SSL

Reverse-proxy с nginx на 8000-й порт + Let's Encrypt:

```nginx
server {
    server_name llmappmarket.com www.llmappmarket.com;
    listen 443 ssl http2;
    ssl_certificate     /etc/letsencrypt/live/llmappmarket.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/llmappmarket.com/privkey.pem;

    client_max_body_size 10m;

    location /static/ { alias /opt/llmmarket/staticfiles/; }
    location /media/  { alias /opt/llmmarket/media/; }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
server {
    listen 80;
    server_name llmappmarket.com www.llmappmarket.com;
    return 301 https://$host$request_uri;
}
```

---

## Post-deploy smoke checks

После старта контейнеров и настройки nginx прогнать:

### Web smoke (5 секунд)

```bash
curl -fsS https://llmappmarket.com/health/         # → {"status":"ok",...}
curl -fsS https://llmappmarket.com/                # → 200 HTML, есть "Trending now"
curl -fsS https://llmappmarket.com/apps/           # → 200, листинг
curl -fsS https://llmappmarket.com/sitemap.xml     # → 200 XML
curl -fsS https://llmappmarket.com/robots.txt      # → 200
curl -sw '%{http_code}\n' -o /dev/null \
     https://llmappmarket.com/apps/no-such-slug/   # → 404 (ожидаемо)
```

### Admin smoke

1. Открыть `https://llmappmarket.com/admin/`
2. Залогиниться суперпользователем
3. Проверить что доступны: AgentRun, BudgetMonthState, NeedsReviewQueueEntry,
   App, Source, LinkHealth
4. Открыть `https://llmappmarket.com/admin/agent/agentrun/cost-dashboard/`
   — должна отобразиться dashboard с пустым / минимальным spend

### Agent pipeline dry-run smoke

Из контейнера, чтобы не сжечь токены случайно:

```bash
# Gate report (без LLM вызовов — читает БД)
docker compose exec web python manage.py agent_phase3_report

# Cost backfill (dry-run, без записи)
docker compose exec web python manage.py agent_backfill_costs --dry-run

# Бюджетная проверка — пишет/обновляет BudgetMonthState
docker compose exec web python manage.py shell -c \
  "from apps.agent.tasks import agent_budget_check; print(agent_budget_check())"
```

### Beat schedule loaded

```bash
docker compose logs beat | grep "DatabaseScheduler:"
# должно быть: "DatabaseScheduler: Schedule changed."

docker compose exec web python manage.py shell -c \
  "from django_celery_beat.models import PeriodicTask;
   [print(t.name, t.enabled) for t in PeriodicTask.objects.all().order_by('name')]"
```

Ожидаем 10 enabled задач:
- `agent_budget_check`
- `agent_discover_github_mcp`
- `agent_discover_rss`
- `agent_reactualize_apps_batch`
- `agent_review_queue_digest`
- `check_app_links_batch`
- `ingest_mcp_registry`
- `newsletter_draft`
- `rebuild_sitemap`
- `refresh_search_vectors_batch`

---

## Включение background-задач

По умолчанию `AGENT_SOURCES_ENABLED=` и `AGENT_REACTUALIZATION_ENABLED=False` —
никакая агент-задача не делает реальных LLM-вызовов до того как оператор
явно включит её. Это намеренно: первый раз запускать на проде хочется в
наблюдаемом режиме, а не "сам запустился по cron'у в 6:30 утра".

### Шаг 1. Прогнать discovery dry-run вручную

```bash
docker compose exec web python manage.py agent_run --source=github_mcp --limit=5
# Это dry-run по умолчанию — пройдёт через LLM, напишет audit-rows
# в AgentRun / EnrichmentTask / LLMCallLog, но НЕ создаст App.
```

Стоимость: ~$0.01-0.02 на 5 кандидатов.

### Шаг 2. Если результат осмысленный — включить discovery beat

```bash
# В .env:
AGENT_SOURCES_ENABLED=github_mcp,rss
```

```bash
docker compose restart worker beat
```

После этого:
- `discover_github_mcp` запускается Пн/Ср/Пт в 06:30 UTC
- `discover_rss` каждые 6 часов

### Шаг 3. Прогнать re-actualization dry-run

```bash
docker compose exec web python manage.py shell -c \
  "from apps.agent.tasks import reactualize_apps_batch;
   print(reactualize_apps_batch(limit=3, dry_run=True))"
```

### Шаг 4. Включить re-actualization beat

```bash
# В .env:
AGENT_REACTUALIZATION_ENABLED=True
```

```bash
docker compose restart worker beat
```

После этого `reactualize_apps_batch` запускается ежедневно в 07:00 UTC.

### Шаг 5. Проверить budget alerts

Один раз форсировать алерт чтобы убедиться что email уходит:

```bash
# Поднять бюджет до тестового низкого значения
AGENT_MONTHLY_BUDGET_USD=0.10 docker compose restart worker
docker compose exec web python manage.py shell -c \
  "from apps.agent.tasks import agent_budget_check; print(agent_budget_check())"
# Должен прислать письмо что превышен 100% порог.
# Не забыть вернуть AGENT_MONTHLY_BUDGET_USD=20 и сбросить latches в админке.
```

---

## Операции дня 2

### Просмотр логов

```bash
docker compose logs -f web --tail=100         # gunicorn + Django
docker compose logs -f worker --tail=100      # Celery worker (LLM, link checks)
docker compose logs -f beat --tail=100        # beat scheduler
```

### Проверка стоимости / лимитов

Открыть `/admin/agent/agentrun/cost-dashboard/` — там:
- сумма за текущий месяц + бюджет + утилизация %
- per-day breakdown за 30 дней
- per-model + per-source разбивка
- Top 10 самых дорогих AgentRun'ов

### Проверка статуса latches бюджета

`/admin/agent/budgetmonthstate/` — одна строка на месяц. Поля
`discovery_disabled_at` и `hard_stop_at` показывают когда сработали
ограничения (если сработали). Сбросить вручную = очистить таймштампы
+ сохранить запись.

### Очередь редактора

`/admin/agent/needsreviewqueueentry/` — что LLM предложил, но не
применил автоматически. Три категории:
- `enriched` — обогащение существующего DRAFT
- `reactualized` — diff при re-actualization
- `vanished` — линк сдох на 7 проверках подряд

Действия редактора прямо из админки: Apply verdict / Approve & publish /
Reject / Mark resolved.

### Apply изменений кода

```bash
cd /opt/llmmarket
git pull
docker compose build web worker beat
docker compose up -d web worker beat
# Миграции применятся автоматически entrypoint'ом.
```

### Бэкап БД

```bash
# Если postgres внутри docker-compose:
docker compose exec postgres pg_dump -U llmmarket llmmarket > backup-$(date +%F).sql

# Если RDS — снапшоты делать через AWS Console или automated daily snapshots.
```

---

## Rollback

Деплой не накатывает destructive миграций по умолчанию — Phase 1-5
миграции аддитивные (новые таблицы / поля / индексы). Откатить можно
просто переключив код:

```bash
cd /opt/llmmarket
git log --oneline -10                              # найти commit до проблемного
git checkout <good-commit-sha>
docker compose build web worker beat
docker compose up -d web worker beat
```

Если миграция новой версии добавила nullable-поле, старая версия кода
просто его проигнорирует. Если новая миграция добавила NOT NULL —
сделать reverse-migration сначала:

```bash
docker compose exec web python manage.py migrate <app> <previous-migration-name>
```

**Не отказываться от автоматических снапшотов БД** — single source of
recovery если что-то поломалось.

---

## Известные особенности

### npm 403 на link checker

`https://www.npmjs.com/package/...` отвечает 403 на HEAD-запросы от
автоматов. Link checker засчитывает это как fail, но 5 фейлов
недостаточно для auto-deprecate (threshold = 7). В админке
LinkHealth → consecutive_failures=1 — не баг, ничего делать не надо.

### MCP Registry URL drift

На момент написания (2026-05-16) `https://registry.modelcontextprotocol.io/v1/servers`
отвечает 404 — внешний registry, видимо, изменил endpoint. Задача
`ingest_mcp_registry` обрабатывает это gracefully (zero counters, no
crash), но новые записи через MCP Registry не приходят. Phase 3
discovery (`github_mcp`) — основной активный путь, в нём всё работает.

**Action item** для оператора: периодически проверять актуальный URL
MCP Registry и обновлять `MCP_REGISTRY_BASE_URL` если изменился.

### Cached input cost

OpenAI бьёт cached input по ~10% от standard input rate. Pipeline
учитывает это в `_estimate_cost_usd`: `(input - cached) × normal +
cached × cached_price + output × output_price`. Если в будущем
OpenAI изменит ratio или порог cache TTL — обновить
`AGENT_OPENAI_*_CACHED_COST_PER_1M_TOKENS` соответственно и прогнать
`agent_backfill_costs --include-nonzero` для re-pricing исторических
строк.

### F4 broker auto-fallback

При запуске `manage.py agent_run` из host-venv (не из контейнера)
обнаружится что Redis по имени `redis` не резолвится — pipeline
автоматически переключится в `CELERY_TASK_ALWAYS_EAGER=True` и
выпишет warning в stderr. Это поведение для удобства dev'а; в проде
контейнер всегда видит broker, fallback не срабатывает.

### Phase 4 reactualize: use_case noise

LLM каждый прогон формулирует use-case заголовки немного иначе
("Connect to Slack" vs "Connect Slack"). `compute_reactualization` не
зажигает queue entry на use-case-only diff — иначе редактор бы получал
"use cases changed" сообщение на каждое приложение в каждом цикле.
Дельта use_cases всё равно записывается в diff payload и видна в
queue entry когда срабатывает на что-то ещё (поля / capability /
verdict).
