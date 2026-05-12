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

# Create superuser if it doesn't exist
echo "👤 Creating superuser if needed..."
python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(is_superuser=True).exists():
    import os
    User.objects.create_superuser(
        username=os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin'),
        email=os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@llmappmarket.com'),
        password=os.environ.get('DJANGO_SUPERUSER_PASSWORD', 'admin123')
    )
    print('✅ Superuser created')
else:
    print('✅ Superuser already exists')
" || echo "⚠️ Superuser creation failed, continuing..."

# Collect static files for web service
if [[ "${1:-}" == "gunicorn"* ]]; then
    echo "📦 Collecting static files..."
    python manage.py collectstatic --noinput --clear || echo "⚠️ Static collection failed, continuing..."
fi

echo "🎉 Initialization complete! Starting: $*"
exec "$@"
