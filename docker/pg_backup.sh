#!/usr/bin/env bash
# Periodic pg_dump for the prod-profile pg_backup container.
# Loops forever: dump → upload (optional) → trim retention → sleep.
# Reads the same DATABASE_URL the rest of the stack uses.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${PG_BACKUP_DIR:=/backups}"
: "${PG_BACKUP_RETENTION_DAYS:=14}"
: "${PG_BACKUP_INTERVAL_SECONDS:=86400}"  # 24h

mkdir -p "$PG_BACKUP_DIR"

# Parse DATABASE_URL into PG* env vars expected by pg_dump.
python3 - <<'PY' > /tmp/pg_env
import os
from urllib.parse import urlparse
u = urlparse(os.environ["DATABASE_URL"])
print(f"export PGHOST={u.hostname}")
print(f"export PGPORT={u.port or 5432}")
print(f"export PGUSER={u.username}")
print(f"export PGPASSWORD={u.password}")
print(f"export PGDATABASE={u.path.lstrip('/')}")
PY
# shellcheck disable=SC1091
source /tmp/pg_env

echo "🔁 pg_backup loop: dir=$PG_BACKUP_DIR retention=${PG_BACKUP_RETENTION_DAYS}d interval=${PG_BACKUP_INTERVAL_SECONDS}s"

while true; do
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    out="$PG_BACKUP_DIR/llmmarket-${ts}.sql.gz"

    echo "📦 dumping → $out"
    if pg_dump --no-owner --no-privileges --clean --if-exists \
        "$PGDATABASE" 2>/dev/null | gzip -9 > "$out.tmp"; then
        mv "$out.tmp" "$out"
        echo "✅ dump complete ($(du -h "$out" | cut -f1))"
    else
        rm -f "$out.tmp"
        echo "❌ pg_dump failed — keeping previous backups, will retry next cycle"
    fi

    # Optional S3 upload — only fires when AWS env vars are set. The
    # local dump is retained either way so a misconfigured upload doesn't
    # cost us forensics.
    if [[ -n "${PG_BACKUP_S3_BUCKET:-}" && -f "$out" ]]; then
        echo "☁️  uploading to s3://$PG_BACKUP_S3_BUCKET/"
        if aws s3 cp "$out" "s3://$PG_BACKUP_S3_BUCKET/$(basename "$out")" \
            --only-show-errors; then
            echo "✅ s3 upload complete"
        else
            echo "❌ s3 upload failed — local copy preserved"
        fi
    fi

    # Retention: trim local copies older than the configured window.
    find "$PG_BACKUP_DIR" -maxdepth 1 -name 'llmmarket-*.sql.gz' \
        -mtime "+${PG_BACKUP_RETENTION_DAYS}" -print -delete | sed 's/^/🗑  /' || true

    echo "💤 sleeping ${PG_BACKUP_INTERVAL_SECONDS}s"
    sleep "$PG_BACKUP_INTERVAL_SECONDS"
done
