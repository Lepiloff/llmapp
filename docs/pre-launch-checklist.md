# Pre-launch чек-лист — non-code блокеры

Код прошёл Sprint 1-3 + smoke-test, технически готов к деплою.
Этот документ собирает **операционные / юридические / бизнес**
задачи которые остались. Структурирован по фазам: фаза N разблокирует
фазу N+1. Каждый пункт можно закрывать независимо.

## Status — 2026-05-20

| Фаза | Статус |
|---|---|
| 0 — Pre-flight (legal, домен, инфра) | 🔴 не начато |
| 1 — Soft launch (catalog видим, discovery off) | блокируется фазой 0 |
| 2 — Discovery on (LLM-pipeline активен) | блокируется фазой 1; Sprint 4/5 direct-ingest MVP готов |
| 3 — Growth (контент, monetization, ChatGPT Apps) | блокируется фазой 2 |

---

## Фаза 0 — Pre-flight 🔴 (блокеры релиза)

Без этих пунктов прод-сайт нельзя поднимать.

### 0.1 Юридические страницы сайта
- [ ] Privacy Policy опубликована (`/privacy/`). Должна описывать что мы храним: email подписчиков newsletter'а, IP+user-agent в `ClickEvent` / `PageView` / `SearchLog`, submitter_ip в `Submission` / `ClaimRequest`.
- [ ] Terms of Service опубликованы (`/terms/`).
- [ ] Cookie consent banner ИЛИ truncation IP до /16 уровня в `apps.analytics.utils.get_client_ip` (тогда баннер не нужен под GDPR).
- [ ] Footer-ссылки на `/privacy/` + `/terms/` в `templates/base.html`.

**Owner:** legal-консультант + dev (10 минут на templates).
**Зависит от:** ничего.

### 0.2 Домен + DNS + HTTPS
- [ ] Домен `llmappmarket.com` куплен и в управлении.
- [ ] A-records: `llmappmarket.com` + `www.llmappmarket.com` → IP EC2 (или Cloudflare proxy).
- [ ] TLS-сертификат:
  - Вариант A (рекомендую): Cloudflare proxy = TLS на их стороне, EC2 отвечает HTTP. Уже использован для Turnstile, так что один меньше внешний сервис.
  - Вариант B: Caddy на EC2 = auto Let's Encrypt + renewal без cron.
  - Вариант C: nginx + certbot + cron-job на renewal.
- [ ] `SECURE_SSL_REDIRECT` + `SECURE_PROXY_SSL_HEADER` поправлены под выбранную схему.

**Owner:** devops/admin.
**Зависит от:** ничего.

### 0.3 Реальные credentials в `.env.production`
Все плейсхолдеры заменить на боевые значения:

- [ ] `SECRET_KEY` — сгенерирован высоко-энтропийный (`python -c 'import secrets; print(secrets.token_urlsafe(64))'`). Прод бутится с hard-fail если оставить дефолт.
- [ ] `OPENAI_API_KEY` — реальный sk-… key.
- [ ] **Спам-лимит на стороне OpenAI dashboard** (отдельная от наших `AGENT_MONTHLY_BUDGET_USD` защита — на случай если наш latch не сработает).
- [ ] `GITHUB_TOKEN` — fine-grained PAT, scope: read-only public repos.
- [ ] `TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET_KEY` — проект в Cloudflare Turnstile привязан к домену.
- [ ] `SENTRY_DSN` — Sentry-проект создан, retention выбран.
- [ ] `EMAIL_*` — transactional-провайдер (SES / SendGrid / Postmark) настроен, DKIM/SPF записи в DNS прописаны.
- [ ] `DJANGO_SUPERUSER_*` — первый editor-аккаунт. После первого boot переменные можно убрать.

**Owner:** admin.
**Зависит от:** 0.2 (домен).

### 0.4 Реальные домены в env
- [ ] `SITE_BASE_URL=https://llmappmarket.com`
- [ ] `ALLOWED_HOSTS=llmappmarket.com,www.llmappmarket.com`
- [ ] `CSRF_TRUSTED_ORIGINS=https://llmappmarket.com,https://www.llmappmarket.com`
- [ ] `CORS_ALLOWED_ORIGINS=` (пусто на старте — нет отдельного фронтенда)
- [ ] `AGENT_REVIEW_DIGEST_EMAILS` / `AGENT_BUDGET_ALERT_EMAILS` / `SUBMISSIONS_NOTIFY_EMAILS` — реальные адреса.

**Owner:** admin. **Зависит от:** 0.2.

### 0.5 Off-host backup
- [ ] AWS account / DO Spaces / Backblaze B2 — bucket создан.
- [ ] IAM-юзер с write-only-разрешением на bucket.
- [ ] `PG_BACKUP_S3_BUCKET` + `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` + `AWS_DEFAULT_REGION` в `.env.production`.
- [ ] Восстановление протестировано: скачать дамп → gunzip → `psql llmmarket < dump.sql` в staging.

**Owner:** devops.
**Зависит от:** 0.3.

### 0.6 OG image — продакшен-версия
- [ ] `static/img/og-default.png` (сейчас placeholder из Sprint 3 smoke fix) заменён на дизайнерский 1200×630 PNG.

**Owner:** designer.
**Зависит от:** ничего. Не блокер — placeholder работает.

### 0.7 Editorial-роль определена
- [ ] Кто конкретный человек смотрит `/admin/agent/needsreviewqueueentry/`?
- [ ] Договорённость на cadence (ежедневно? через день? раз в неделю?). На старте discovery off — нагрузка минимальна.
- [ ] Этот email в `AGENT_REVIEW_DIGEST_EMAILS`.

**Owner:** product owner.
**Зависит от:** ничего.

---

## Фаза 1 — Soft launch 🟡 (после 0)

Сайт работает, каталог виден, discovery всё ещё выключен — наблюдаем
behavior в продакшен-окружении на готовых 24 карточках.

### 1.1 Деплой
- [ ] Pre-deploy backup текущей dev-БД (если миграция dev-данных).
- [ ] `docker compose --profile production up -d --build`.
- [ ] Все шаги из `deployment-ru.md` § Post-deploy smoke checks.
- [ ] Verify CSP в браузере: открыть `/`, DevTools → console → нет красных CSP-violations.

### 1.2 Search engine indexing
- [ ] Google Search Console: добавить domain property, verify через DNS TXT, submit sitemap.xml.
- [ ] Bing Webmaster: то же самое.
- [ ] `robots.txt` отдаёт корректный allowlist.

### 1.3 Monitoring / alerting wired
- [ ] Sentry получает первый sentinel error (форсировать `1/0` через shell).
- [ ] `agent_budget_check` форсирован с low budget → email пришёл.
- [ ] `send_review_queue_digest` форсирован → email пришёл (с пустой очередью — это OK).
- [ ] `pg_backup` контейнер в production-profile работает → первый дамп лежит в S3.

### 1.4 Operator решения по env-флагам
- [ ] `AGENT_MONTHLY_BUDGET_USD`: оставить $20 или поднять до $50-100 для наполнения каталога в первый квартал?
- [ ] `AGENT_REACTUALIZATION_ENABLED`: оставить False или включить сейчас? (если каталог 24 апп, через 30 дней начнётся re-actualization цикл — стоит включить чтобы не stale'ел).

---

## Фаза 2 — Discovery on 🟢 (после 1)

Включаем LLM-pipeline. После 1-2 недель soft launch'а — наблюдаем что
ничего не сломалось, sentry чистый, бюджет не утекает.

### 2.1 Dry-run перед включением
- [ ] `manage.py agent_run --source=github_mcp --limit=5` (dry-run). Cost <$0.02.
- [ ] Проверить `/admin/agent/agentrun/` — последний run закончился `succeeded`.
- [ ] Проверить `/admin/agent/needsreviewqueueentry/` — кандидаты выглядят осмысленно.

### 2.2 Включить discovery
- [ ] `AGENT_SOURCES_ENABLED=github_mcp,rss,gemini_extensions,claude_connectors,chatgpt_apps` в env.
- [ ] `docker compose restart worker beat`.
- [ ] Beat schedule:
  - `discover_github_mcp` Пн/Ср/Пт 06:30 UTC
  - `discover_rss` каждые 6 часов
  - `ingest_gemini_extensions` daily 04:30 UTC
  - `ingest_claude_connectors` Tuesday 04:45 UTC
  - `ingest_chatgpt_apps` Wednesday 04:45 UTC
- [ ] Before production beat: Phase A pilot уже пройден локально; повторить на prod/staging через `agent_run --source=gemini_extensions --limit=30 --apply`, `agent_run --source=claude_connectors --limit=5 --apply`, `agent_run --source=chatgpt_apps --limit=10 --apply`.

### 2.3 Editorial-cadence
- [ ] Первый rotated digest пришёл editor'у (07:30 UTC). Если очередь >0 — editor'нул хотя бы 3 entries чтобы померить acceptance rate.
- [ ] SLA-дашборд `/admin/agent/needsreviewqueueentry/sla-dashboard/` показывает «OK» (oldest pending <14 дней).

### 2.4 MCP Registry watch
- [ ] Если registry 404 продолжается: связаться с MCP-командами (Anthropic / OpenAI) напрямую, выяснить новый endpoint. Sprint 2 Sentry-alert ловит это автоматически.
- [ ] Альтернативно: пометить MCP Registry источник `is_active=False` и положиться только на GitHub-MCP discovery.

---

## Фаза 3 — Growth 🔵 (после стабилизации)

Открываем второй фронт — official directories, контент, monetization.

### 3.1 B3 — Official directories / ChatGPT Apps
- [x] Gemini Extensions direct-ingest реализован через официальный JSON feed.
- [x] Claude Connectors direct-ingest реализован с robots.txt enforcement и conservative HTML crawl.
- [x] ChatGPT Apps MVP реализован через сторонний crawlable index `mcpapp.net/chatgpt-apps` без Playwright; source rows помечаются как неофициальные.
- [ ] Production rollout: включить Gemini/Claude/ChatGPT flags только после pilot review в админке.
- [ ] OpenAI official ChatGPT App Directory остаётся отдельным hardening item: нужен ToS/partner-channel review перед official-source или Playwright route.

### 3.2 F1 — Anthropic provider
- [ ] Решение по timeline (memory note: primary = Claude Sonnet 4.7, cheap = gpt-5-mini, не использовать Haiku).
- [ ] Implementation в `apps/agent/llm/client.py::AnthropicProvider`. Provider-registry pattern из Sprint 3 делает это однострочной регистрацией.
- [ ] Re-baseline cost-model с Claude Sonnet на primary (текущий $0.006/published-app может уменьшиться на 30-50% за счёт prompt caching).

### 3.3 Контент / SEO
- [ ] Дизайнерский OG-image (см. 0.6).
- [ ] 2-3 editorial-статьи в `/blog/` для SEO:
  - «Top 10 MCP servers for code review» (или аналог)
  - «What is the Claude Connector ecosystem?»
  - «How to submit your app to LLM App Market»
- [ ] Internal linking из статей → карточки каталога.

### 3.4 Submissions hardening
- [ ] Anti-spam beyond Turnstile: honeypot field в `apps/submissions/forms.py::SubmissionForm`.
- [ ] Verification policy для `ClaimRequest`:
  - Простой baseline: email с домена developer'а должен совпадать с `App.developer_url` domain'ом.
  - Усиленный: DNS TXT-record или GitHub repo `.well-known/llm-app-market.txt`.
- [ ] Документировать policy на странице `/submit/` чтобы spammer'ам было меньше мотивации.

### 3.5 Monetization roadmap
- [ ] Featured listings: policy + price tier.
- [ ] Paid API tier: что-то выше free 100 req/page_size?
- [ ] Editorial sponsorships.

---

## Текущие unblock'и

**Что я (engineering) могу подготовить как заготовку, не дожидаясь решений:**

- Шаблон письма legal-owner'у по B3 (3.1) — могу написать.
- Шаблон Privacy Policy / ToS на базе того что реально логируем (0.1) — могу написать draft, до final legal-review.
- IP truncation в `get_client_ip` (0.1 альтернатива cookie banner'у) — 5 строк кода.
- Honeypot field в SubmissionForm (3.4) — 10 строк кода.

**Что блокируется людьми / внешними сервисами:**

- 0.2 домен + DNS + TLS (devops)
- 0.3 / 0.4 реальные credentials и домены (admin)
- 0.5 off-host backup (devops + AWS account)
- 0.6 OG image (designer)
- 0.7 editorial-cadence (product owner)
- 3.1 legal review для B3 (юрист)
- 3.3 контент (editor / writer)
