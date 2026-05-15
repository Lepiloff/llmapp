#!/usr/bin/env bash
set -euo pipefail

echo "🚀 Starting LLM App Market entrypoint..."

# Wait for PostgreSQL to be ready
echo "⏳ Waiting for PostgreSQL..."
while ! python -c "import psycopg; psycopg.connect('$DATABASE_URL')" 2>/dev/null; do
    echo "PostgreSQL is unavailable - sleeping"
    sleep 1
done
echo "✅ PostgreSQL is ready!"

# Wait for Redis to be ready
echo "⏳ Waiting for Redis..."
while ! python -c "import redis; redis.from_url('$REDIS_URL').ping()" 2>/dev/null; do
    echo "Redis is unavailable - sleeping"
    sleep 1
done
echo "✅ Redis is ready!"

# Initialize PostgreSQL extensions if needed
echo "🔧 Initializing PostgreSQL extensions..."
python -c "
import psycopg
try:
    conn = psycopg.connect('$DATABASE_URL')
    with conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm;')
        cur.execute('CREATE EXTENSION IF NOT EXISTS unaccent;')
    conn.commit()
    conn.close()
    print('✅ PostgreSQL extensions initialized')
except Exception as e:
    print(f'⚠️ Could not initialize extensions: {e}')
" || echo "⚠️ Extension initialization failed, continuing..."

# Run migrations
echo "🔄 Running database migrations..."
python manage.py migrate --noinput

# Seed reference data (platforms, categories, capabilities) and demo apps.
# Idempotent — safe to run on every container start.
echo "🌱 Seeding reference data..."
python manage.py seed_demo || echo "⚠️ Seed failed, continuing..."

# Optional superuser bootstrap.
# Only runs when DJANGO_SUPERUSER_USERNAME / _EMAIL / _PASSWORD are all set
# in the environment. No fallback defaults — a hardcoded admin/admin123
# in image bootstrap is a security risk in production. Operators who want
# automatic bootstrap must set the env vars explicitly; otherwise create
# the superuser interactively with `manage.py createsuperuser`.
echo "👤 Checking superuser bootstrap..."
if [[ -n "${DJANGO_SUPERUSER_USERNAME:-}" && -n "${DJANGO_SUPERUSER_EMAIL:-}" && -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
    if python manage.py shell -c "
import sys
from django.contrib.auth import get_user_model
sys.exit(0 if get_user_model().objects.filter(is_superuser=True).exists() else 1)
" >/dev/null 2>&1; then
        echo "✅ Superuser already exists; skipping bootstrap"
    else
        echo "  Creating superuser '${DJANGO_SUPERUSER_USERNAME}' from env vars..."
        python manage.py createsuperuser --noinput \
            --username "$DJANGO_SUPERUSER_USERNAME" \
            --email "$DJANGO_SUPERUSER_EMAIL"
        echo "✅ Superuser created"
    fi
else
    echo "⏭️  Skipping superuser bootstrap (DJANGO_SUPERUSER_* env vars not set)."
    echo "    Run 'manage.py createsuperuser' to create one when needed."
fi

# Collect static files for web service
if [[ "${1:-}" == "gunicorn"* ]]; then
    echo "📦 Collecting static files..."
    python manage.py collectstatic --noinput --clear || echo "⚠️ Static collection failed, continuing..."
fi

echo "🎉 Initialization complete! Starting: $*"
exec "$@"
