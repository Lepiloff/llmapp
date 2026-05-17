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

# Install Python dependencies from pyproject.toml. Single source of
# truth — historically this Dockerfile carried a hand-curated list
# that drifted: WhiteNoise (Sprint 2) and django-ninja (Sprint 3)
# never made it in, while python-Levenshtein lingered after Sprint 3
# removed it. Using `pip install .` keeps the image deps locked to
# whatever pyproject.toml declares; CI also installs the same list so
# drift is impossible.
#
# pyproject.toml's [tool.setuptools] declares packages = [], so this
# resolves dependencies without trying to bundle the Django app as a
# wheel. README.md is intentionally not COPY'd — .dockerignore excludes
# it and pyproject.toml doesn't reference it, so the install layer
# rebuilds only when dependencies actually change.
COPY pyproject.toml /app/
RUN pip install --upgrade pip && pip install .

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
