"""Analytics app config: registers the ``order_paid`` receivers on import."""

from __future__ import annotations

from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
    verbose_name = "Analytics"

    def ready(self) -> None:  # pragma: no cover - wiring, exercised by the suite
        from apps.analytics import receivers  # noqa: F401
