"""FLASHWEAR Back Office (Phase 16): dashboards, queues and staff operations.

A leaf app: it reads every domain and calls their services, and nothing imports it back
(``/operations/`` is mounted from the root URLconf, and ``/api/v1/backoffice/`` from the
API root). Its only table is the append-only staff audit log -- the one thing the
project did not already have.
"""

from django.apps import AppConfig


class BackofficeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.backoffice"
    verbose_name = "FLASH Operations"
