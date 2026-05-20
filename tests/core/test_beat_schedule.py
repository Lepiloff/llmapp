"""Regressions for the Celery beat schedule.

The schedule entries live in ``config/settings/base.py``; this test
asserts that every task name referenced in the schedule actually
resolves to a registered Celery task. Catches typos and rename drift
that would otherwise only surface in a stack trace at the next firing.
"""
from __future__ import annotations

import importlib

import pytest
from django.conf import settings


REQUIRED_TASKS = {
    "apps.sources.tasks.ingest_mcp_registry",
    "apps.sources.tasks.check_app_links_batch",
    "apps.sources.tasks.cleanup_old_link_check_results",
    "apps.search.tasks.refresh_search_vectors_batch",
    "apps.search.tasks.update_popular_searches",
    "apps.search.tasks.cleanup_old_search_logs",
    "apps.seo.tasks.rebuild_sitemap",
    "apps.seo.tasks.ping_search_engines",
    "apps.seo.tasks.generate_seo_reports",
    "apps.analytics.tasks.calculate_trending_scores",
    "apps.analytics.tasks.cleanup_old_analytics_data",
    "apps.catalog.tasks.recalc_quality_scores_batch",
    "apps.newsletter.tasks.create_weekly_draft",
    "apps.agent.tasks.send_review_queue_digest",
    "apps.agent.tasks.discover_rss",
    "apps.agent.tasks.discover_github_mcp",
    "apps.agent.tasks.ingest_gemini_extensions",
    "apps.agent.tasks.ingest_claude_connectors",
    "apps.agent.tasks.reactualize_apps_batch",
    "apps.agent.tasks.agent_budget_check",
    "apps.agent.tasks.cleanup_old_agent_logs",
}


def test_all_required_tasks_are_scheduled() -> None:
    scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    missing = REQUIRED_TASKS - scheduled
    assert not missing, f"Missing tasks in beat: {sorted(missing)}"


@pytest.mark.parametrize("dotted_name", sorted(REQUIRED_TASKS))
def test_scheduled_task_is_importable(dotted_name: str) -> None:
    module_path, _, attr = dotted_name.rpartition(".")
    module = importlib.import_module(module_path)
    task = getattr(module, attr, None)
    assert task is not None, f"{dotted_name} not found in {module_path}"
    # @shared_task decorator yields a callable with a ``delay`` method.
    assert callable(task), f"{dotted_name} is not callable"
