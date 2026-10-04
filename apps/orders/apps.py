"""FLASHWEAR orders: the checkout handoff, the order lifecycle and shipments."""

from __future__ import annotations

from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orders"
    verbose_name = "FLASHWEAR Orders"
