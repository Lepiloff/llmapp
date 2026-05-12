"""Sources background tasks for ingestion and link checking.

Architecture refs:
  * docs/architecture.md § 9 (sources & ingest)
  * docs/architecture.md § 12.1 (background tasks)
"""
from __future__ import annotations

import logging
import requests
from typing import Dict, List
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.catalog.models import App
from .models import Source, LinkCheckResult, LinkHealth, UnparsedRegistryRecord

logger = logging.getLogger(__name__)


@shared_task
def ingest_mcp_registry() -> Dict[str, int]:
    """Ingest MCP Registry and create/update apps.

    This task fetches the MCP registry and creates draft App records
    for new entries.
    """
    from django.conf import settings

    created_count = 0
    updated_count = 0
    error_count = 0

    try:
        registry_url = settings.MCP_REGISTRY_BASE_URL
        response = requests.get(f"{registry_url}/servers", timeout=30)
        response.raise_for_status()

        servers_data = response.json()

        for server_data in servers_data.get('servers', []):
            try:
                with transaction.atomic():
                    result = _process_mcp_server(server_data)
                    if result == 'created':
                        created_count += 1
                    elif result == 'updated':
                        updated_count += 1

            except Exception as e:
                logger.error(f"Error processing MCP server {server_data.get('name', 'unknown')}: {e}")
                error_count += 1

                # Store unparsed record for manual review
                UnparsedRegistryRecord.objects.create(
                    payload=server_data,
                    error=str(e)[:1000],
                    schema_version="1.0"
                )

        result = {
            'created_count': created_count,
            'updated_count': updated_count,
            'error_count': error_count,
        }

        logger.info(f"MCP Registry ingest completed: {result}")
        return result

    except Exception as e:
        logger.error(f"MCP Registry ingest failed: {e}")
        raise


@shared_task
def check_app_links_batch(batch_size: int = 50) -> Dict[str, int]:
    """Check app links for health and update LinkHealth records."""
    from apps.sources.models import LinkCheckResult

    checked_count = 0
    failed_count = 0

    try:
        # Get apps that haven't been checked recently
        cutoff = timezone.now() - timedelta(days=1)
        apps_to_check = (
            App.published.all()
            .filter(last_checked_at__lt=cutoff)
            .order_by('last_checked_at')[:batch_size]
        )

        for app in apps_to_check:
            try:
                _check_app_links(app)
                checked_count += 1

                # Update last checked timestamp
                App.objects.filter(pk=app.pk).update(last_checked_at=timezone.now())

            except Exception as e:
                logger.error(f"Error checking links for app {app.pk}: {e}")
                failed_count += 1

        result = {
            'checked_count': checked_count,
            'failed_count': failed_count,
        }

        logger.info(f"Link check batch completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Link check batch failed: {e}")
        raise


def _process_mcp_server(server_data: dict) -> str:
    """Process a single MCP server from the registry."""
    name = server_data.get('name', '').strip()
    if not name:
        raise ValueError("Server name is required")

    external_id = server_data.get('id') or name

    # Check if we already have this server
    source, created = Source.objects.get_or_create(
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id=external_id,
        defaults={
            'source_url': server_data.get('url', ''),
            'payload': server_data,
            'is_active': True,
            'is_primary': True,
        }
    )

    if created:
        # Create new App from MCP server data
        app = _create_app_from_mcp_server(server_data, source)
        logger.info(f"Created new app from MCP server: {app.name}")
        return 'created'
    else:
        # Update existing source
        source.payload = server_data
        source.source_url = server_data.get('url', '')
        source.fetched_at = timezone.now()
        source.save()

        # Update app if needed
        if source.app:
            _update_app_from_mcp_server(source.app, server_data)
            logger.info(f"Updated app from MCP server: {source.app.name}")

        return 'updated'


def _create_app_from_mcp_server(server_data: dict, source: Source) -> App:
    """Create a new App from MCP server data."""
    from django.utils.text import slugify

    name = server_data.get('name', '').strip()
    description = server_data.get('description', '')[:280]

    # Create base slug
    slug = slugify(name)[:200]

    # Ensure slug is unique
    counter = 1
    original_slug = slug
    while App.objects.filter(slug=slug).exists():
        slug = f"{original_slug}-{counter}"[:200]
        counter += 1

    app = App.objects.create(
        name=name,
        slug=slug,
        short_description=description,
        long_description=server_data.get('long_description', ''),
        official_page_url=server_data.get('homepage', ''),
        repo_url=server_data.get('repository', ''),
        install_url=server_data.get('install_url', ''),
        developer_name=server_data.get('author', ''),
        status=App.AppStatus.DRAFT,  # Always start as draft
        platform_verification_status=App.PlatformVerificationStatus.NOT_LISTED,
        pricing_model=App.PricingModel.FREE,  # MCP servers are typically free
    )

    # Link the source to the app
    source.app = app
    source.save()

    return app


def _update_app_from_mcp_server(app: App, server_data: dict) -> None:
    """Update existing App with fresh MCP server data."""
    # Only update if the app is still in draft or the data is significantly different
    if app.status == App.AppStatus.PUBLISHED:
        # Only update non-editorial fields for published apps
        fields_to_update = []

        if app.repo_url != server_data.get('repository', ''):
            app.repo_url = server_data.get('repository', '')
            fields_to_update.append('repo_url')

        if app.official_page_url != server_data.get('homepage', ''):
            app.official_page_url = server_data.get('homepage', '')
            fields_to_update.append('official_page_url')

        if fields_to_update:
            app.save(update_fields=fields_to_update)
    else:
        # Update all fields for draft apps
        app.short_description = server_data.get('description', '')[:280]
        app.long_description = server_data.get('long_description', '')
        app.official_page_url = server_data.get('homepage', '')
        app.repo_url = server_data.get('repository', '')
        app.install_url = server_data.get('install_url', '')
        app.developer_name = server_data.get('author', '')
        app.save()


def _check_app_links(app: App) -> None:
    """Check all links for a single app and update LinkHealth."""
    links_to_check = [
        ('official', app.official_page_url),
        ('install', app.install_url),
        ('repo', app.repo_url),
    ]

    # Add platform directory links
    for platform_link in app.platform_links.all():
        if platform_link.official_directory_url:
            links_to_check.append(('directory', platform_link.official_directory_url))

    for link_type, url in links_to_check:
        if not url:
            continue

        try:
            start_time = timezone.now()
            response = requests.head(url, timeout=10, allow_redirects=True)
            duration_ms = int((timezone.now() - start_time).total_seconds() * 1000)

            # Record the check
            check_result = LinkCheckResult.objects.create(
                app=app,
                target=link_type,
                url=url,
                status_code=response.status_code,
                ok=response.status_code < 400,
                duration_ms=duration_ms,
            )

            # Update health record
            _update_link_health(app, link_type, url, check_result.ok, response.status_code)

        except Exception as e:
            # Record failed check
            LinkCheckResult.objects.create(
                app=app,
                target=link_type,
                url=url,
                ok=False,
                error_message=str(e)[:300],
            )

            # Update health record
            _update_link_health(app, link_type, url, False, None)


def _update_link_health(app: App, target: str, url: str, ok: bool, status_code: int = None) -> None:
    """Update or create LinkHealth record."""
    health, created = LinkHealth.objects.get_or_create(
        app=app,
        target=target,
        defaults={
            'url': url,
            'consecutive_failures': 0 if ok else 1,
            'last_status_code': status_code,
            'last_ok_at': timezone.now() if ok else None,
            'last_failed_at': None if ok else timezone.now(),
        }
    )

    if not created:
        health.url = url
        health.last_status_code = status_code

        if ok:
            health.consecutive_failures = 0
            health.last_ok_at = timezone.now()
        else:
            health.consecutive_failures += 1
            health.last_failed_at = timezone.now()

            # Auto-deprecate if too many failures
            if health.consecutive_failures >= 5:
                # Mark app as deprecated if critical link fails repeatedly
                if target in ('official', 'install'):
                    App.objects.filter(pk=app.pk).update(
                        launch_status=App.LaunchStatus.DEPRECATED
                    )
                    logger.warning(f"Auto-deprecated app {app.pk} due to {health.consecutive_failures} failures on {target}")

        health.save()