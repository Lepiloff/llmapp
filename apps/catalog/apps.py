from django.apps import AppConfig


class CatalogConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.catalog"
    verbose_name = "Catalog"

    def ready(self) -> None:
        # Wire signal receivers (search-vector refresh, etc.)
        from . import signals  # noqa: F401
