#!/usr/bin/env bash
set -euo pipefail

# Populate a fresh production database from network sources. Every imported
# listing remains a DRAFT until an editor publishes it through Django admin.
dc=(docker compose)
direct_limit="${DIRECT_INGEST_LIMIT:-500}"
gemini_limit="${GEMINI_EXTENSIONS_LIMIT:-$direct_limit}"
claude_limit="${CLAUDE_CONNECTORS_LIMIT:-$direct_limit}"
chatgpt_limit="${CHATGPT_APPS_LIMIT:-$direct_limit}"

run_source() {
    local source="$1"
    shift
    echo "==> ingesting ${source}"
    "${dc[@]}" exec -T web \
        python manage.py agent_run --source="${source}" --apply "$@"
}

run_source mcp_registry
run_source gemini_extensions --limit="${gemini_limit}"
run_source claude_connectors --limit="${claude_limit}"
run_source chatgpt_apps --limit="${chatgpt_limit}"

echo "==> catalog summary"
"${dc[@]}" exec -T web python manage.py shell -c "
from django.db.models import Count
from apps.catalog.models import App
from apps.sources.models import Source
print('apps:', dict(App.objects.values_list('status').annotate(count=Count('id'))))
print('sources:', dict(Source.objects.values_list('source_type').annotate(count=Count('id'))))
"
