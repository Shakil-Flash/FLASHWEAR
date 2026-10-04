"""FLASHWEAR inventory: stock levels, the movement ledger and checkout holds."""

from __future__ import annotations

from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inventory"
    verbose_name = "FLASHWEAR Inventory"
