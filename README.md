# 🚀 LLM App Market

> **Directory of apps, connectors and MCP servers for ChatGPT, Claude, Gemini and beyond.**

A Django-powered catalog with faceted search, automatic ingestion, submission workflows, and editorial features.

## ⚡ Quick Start (Docker)

### Prerequisites
- Docker & Docker Compose
- Git

### 1. Clone and Setup

```bash
git clone <your-repo-url>
cd llmmarket

# Copy environment file and customize if needed
cp .env .env.local
```

### 2. Start Services

```bash
# Start all services (PostgreSQL, Redis, Django, Celery)
docker-compose up -d

# Watch logs
docker-compose logs -f web worker beat
```

### 3. Access the Application

- **Web App**: http://localhost:8000
- **Django Admin**: http://localhost:8000/admin
  - Username: `admin`
  - Password: `admin123` (change in production!)

### 4. Initialize Data

```bash
# Ingest MCP Registry (optional)
docker-compose exec web python manage.py shell -c "from apps.sources.tasks import ingest_mcp_registry; ingest_mcp_registry()"

# Or using make
docker-compose exec web make ingest
```

## 🏗 Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   PostgreSQL    │    │      Redis      │    │     Django      │
│  (with pg_trgm) │    │ (cache & queue) │    │ (web + worker)  │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                        │                        │
         └────────────────────────┼────────────────────────┘
                                  │
                         ┌─────────────────┐
                         │  Celery Beat    │
                         │ (periodic tasks)│
                         └─────────────────┘
```

### Tech Stack
- **Backend**: Django 5.x + PostgreSQL 16 + Redis 7
- **Search**: PostgreSQL full-text search + trigrams
- **Queue**: Celery with Redis broker
- **Frontend**: Django templates + HTMX + Tailwind CSS _(coming soon)_
- **Deploy**: Docker + Docker Compose

## 📁 Project Structure

```
├── apps/                   # Django applications
│   ├── catalog/           # Core app catalog models
│   ├── search/            # Search & filtering
│   ├── sources/           # Data ingestion (MCP Registry, etc)
│   ├── submissions/       # User submissions & claims
│   ├── analytics/         # Click tracking & trends
│   ├── editorial/         # Blog posts & collections
│   ├── newsletter/        # Email campaigns
│   ├── seo/              # SEO & structured data
│   └── core/             # Shared utilities
├── config/               # Django settings & URLs
├── docker/               # Docker configuration
├── docs/                 # Architecture & business docs
├── static/               # Static assets _(to be implemented)_
├── templates/            # Django templates _(to be implemented)_
└── manage.py
```

## 🔧 Development

### Running Tests
```bash
docker-compose exec web python -m pytest
```

### Database Operations
```bash
# Create migrations
docker-compose exec web python manage.py makemigrations

# Apply migrations
docker-compose exec web python manage.py migrate

# Create superuser
docker-compose exec web python manage.py createsuperuser
```

### Background Tasks
```bash
# Manual task execution
docker-compose exec web python manage.py shell -c "from apps.sources.tasks import check_app_links_batch; check_app_links_batch()"

# Check celery status
docker-compose exec worker celery -A config inspect active
```

### Database Shell
```bash
# Django shell
docker-compose exec web python manage.py shell

# PostgreSQL shell
docker-compose exec postgres psql -U llmmarket -d llmmarket
```

## 🌍 Production Deployment

### With Nginx (Recommended)
```bash
# Start with nginx proxy
docker-compose --profile production up -d

# SSL setup with Let's Encrypt (manual)
# Add your SSL certificates to docker/nginx.conf
```

### Environment Variables

Key variables for production:

```env
DEBUG=False
SECRET_KEY=your-production-secret-key
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com
SITE_BASE_URL=https://yourdomain.com
DJANGO_SETTINGS_MODULE=config.settings.prod

# Database
DATABASE_URL=postgres://user:pass@host:5432/dbname

# Email
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
SMTP_HOST=smtp.yourmailserver.com
# ... other SMTP settings

# External APIs
TURNSTILE_SITE_KEY=your-cloudflare-turnstile-site-key
TURNSTILE_SECRET_KEY=your-cloudflare-turnstile-secret

# Monitoring
SENTRY_DSN=your-sentry-dsn
```

## 🎯 Key Features

### 📱 Catalog & Search
- **Multi-platform support**: ChatGPT, Claude, Gemini, MCP, etc.
- **Faceted search**: Filter by platform, category, capabilities
- **Fuzzy matching**: PostgreSQL trigrams for typo tolerance
- **Quality scoring**: Editorial review + platform verification

### 🔄 Data Ingestion
- **MCP Registry**: Automatic sync with Model Context Protocol registry
- **Link monitoring**: Health checks with auto-deprecation
- **User submissions**: Community-driven content with moderation

### ✏ Editorial Tools
- **Admin interface**: Full Django admin for content management
- **Blog system**: Posts, collections, app comparisons
- **Newsletter**: Email campaigns with open/click tracking

### 📊 Analytics
- **Click tracking**: Outbound link monitoring
- **Trending algorithms**: Time-weighted popularity scores
- **Search analytics**: Query logging and suggestions

## 🛠 API Endpoints

### Public API
- `GET /` - Home page with featured apps
- `GET /apps/` - Faceted search interface
- `GET /apps/{slug}/` - App detail page
- `GET /apps/{category}/` - Category pages
- `GET /{platform}/` - Platform pages

### Admin API
- `GET /admin/` - Django admin interface
- `GET /health/` - Health check endpoint

### Background Tasks
- MCP Registry ingestion (daily at 4:00 AM)
- Link health checks (daily at 5:00 AM)
- Search vector refresh (daily at 3:00 AM)
- Sitemap rebuild (every 30 minutes)
- Newsletter drafts (Fridays at 6:00 AM)

## 📚 Documentation

- [`docs/project-overview-ru.md`](docs/project-overview-ru.md) - current owner-facing status and operating model
- [`docs/business.md`](docs/business.md) - product scope, taxonomy, editorial rules, source policy
- [`docs/architecture.md`](docs/architecture.md) - backend architecture and model contracts
- [`docs/agent-pipeline.md`](docs/agent-pipeline.md) - agent/discovery invariants and rollout gates
- [`docs/deployment-ru.md`](docs/deployment-ru.md) - production deployment and operations runbook
- [`docs/pre-launch-checklist.md`](docs/pre-launch-checklist.md) - launch checklist and remaining non-code blockers

Historical brainstorms, rollout logs, and completed implementation plans are kept in git history instead of `docs/`.

## 🤝 Contributing

1. **Fork the repository**
2. **Create feature branch**: `git checkout -b feature/amazing-feature`
3. **Run tests**: `docker-compose exec web python -m pytest`
4. **Commit changes**: `git commit -m 'Add amazing feature'`
5. **Push branch**: `git push origin feature/amazing-feature`
6. **Create Pull Request**

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🎉 Next Steps

**Backend is complete!** Ready for Frontend implementation:

1. **Django templates** with HTMX integration
2. **Tailwind CSS** styling
3. **Alpine.js** for interactive components
4. **Progressive enhancement** with minimal JavaScript

---

**Built with ❤️ for the LLM community**
