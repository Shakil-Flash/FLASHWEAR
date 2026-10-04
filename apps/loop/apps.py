"""FLASH Loop (Phase 13): resale, trade-in and recycling.

The app is deliberately a leaf in the migration graph: it reads the catalogue,
the closet, orders and engagement, but nothing above it imports it. That keeps
the circular-fashion layer additive -- it can be disabled without touching the
commerce core it wraps.
"""

from django.apps import AppConfig


class LoopConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.loop"
    verbose_name = "FLASH Loop"
