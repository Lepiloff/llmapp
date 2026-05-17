#!/usr/bin/env bash
# Periodic pg_dump for the prod-profile pg_backup container.
# Loops forever: dump → upload (optional) → trim retention → sleep.
# Reads the same DATABASE_URL the rest of the stack uses; pg_dump 16+
# accepts a postgresql://… URL directly, so we don't have to parse and
# re-emit shell `export` statements (which corrupts passwords containing
# spaces, `#`, `$`, quotes, or percent-encoded chars).
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${PG_BACKUP_DIR:=/backups}"
: "${PG_BACKUP_RETENTION_DAYS:=14}"
: "${PG_BACKUP_INTERVAL_SECONDS:=86400}"  # 24h

mkdir -p "$PG_BACKUP_DIR"

echo "🔁 pg_backup loop: dir=$PG_BACKUP_DIR retention=${PG_BACKUP_RETENTION_DAYS}d interval=${PG_BACKUP_INTERVAL_SECONDS}s"

while true; do
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    out="$PG_BACKUP_DIR/llmmarket-${ts}.sql.gz"

    echo "📦 dumping → $out"
    # Pass DATABASE_URL straight to pg_dump. libpq parses the URI itself,
    # so percent-encoded chars in the password are honored without going
    # through a shell-quoting layer.
    if pg_dump --no-owner --no-privileges --clean --if-exists \
        "$DATABASE_URL" 2>/dev/null | gzip -9 > "$out.tmp"; then
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
        if command -v aws >/dev/null 2>&1; then
            echo "☁️  uploading to s3://$PG_BACKUP_S3_BUCKET/"
            if aws s3 cp "$out" "s3://$PG_BACKUP_S3_BUCKET/$(basename "$out")" \
                --only-show-errors; then
                echo "✅ s3 upload complete"
            else
                echo "❌ s3 upload failed — local copy preserved"
            fi
        else
            echo "⚠️  PG_BACKUP_S3_BUCKET set but 'aws' CLI not present — skipping upload"
        fi
    fi

    # Retention: trim local copies older than the configured window.
    find "$PG_BACKUP_DIR" -maxdepth 1 -name 'llmmarket-*.sql.gz' \
        -mtime "+${PG_BACKUP_RETENTION_DAYS}" -print -delete | sed 's/^/🗑  /' || true

    echo "💤 sleeping ${PG_BACKUP_INTERVAL_SECONDS}s"
    sleep "$PG_BACKUP_INTERVAL_SECONDS"
done
