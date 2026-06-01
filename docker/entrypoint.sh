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

# Serialize first-boot schema work across web, worker and beat. Compose starts
# them concurrently after Postgres becomes healthy; without the advisory lock,
# two migrate processes can race while creating the same PostgreSQL objects.
python docker/bootstrap.py

# Collect static files for web service
if [[ "${1:-}" == "gunicorn"* ]]; then
    echo "📦 Collecting static files..."
    python manage.py collectstatic --noinput --clear || echo "⚠️ Static collection failed, continuing..."
fi

echo "🎉 Initialization complete! Starting: $*"
exec "$@"
