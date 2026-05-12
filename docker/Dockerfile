FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml /app/
RUN pip install --upgrade pip && \
    pip install "Django>=5.0,<6.0" "psycopg[binary]>=3.1,<4.0" python-decouple dj-database-url \
    "celery>=5.3,<6.0" django-celery-beat redis django-redis requests python-Levenshtein django-ratelimit \
    django-cors-headers sentry-sdk python-json-logger gunicorn Pillow

# Copy application code
COPY . /app/

# Create directories for media and static files
RUN mkdir -p /app/media /app/staticfiles

# Copy and set permissions for entrypoint
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Collect static files (with fallback)
RUN python manage.py collectstatic --noinput --clear || echo "Static files collection failed, continuing..."

# Create non-root user for security
RUN groupadd -r django && useradd -r -g django django && \
    chown -R django:django /app
USER django

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
