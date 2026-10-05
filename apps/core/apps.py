"""Core app: cross-cutting concerns shared by every other app.

Holds health checks, site-wide configuration, shared template tags, the logging filter
and the base storefront views. It deliberately contains no commerce logic.
"""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"
    verbose_name = "Core"

    def ready(self) -> None:  # pragma: no cover - exercised at import time
        from apps.core import monitoring

        monitoring.configure()
        monitoring.connect_signals()
