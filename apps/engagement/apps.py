"""FLASHWEAR engagement: reviews, FLASH Points and promotions (Phase 7).

This app is a leaf: it may import the shop, orders and catalogue, while those apps only ever
reference engagement *data* (promotion codes, point amounts) or reach it through a lazy service
import. That keeps the migration graph a tree -- no cycles back from the checkout path.

``ready()`` wires the receivers that turn order events into points movements, mirroring how
``apps.shop`` registers its cart-merge receiver.
"""

from __future__ import annotations

from django.apps import AppConfig


class EngagementConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.engagement"
    verbose_name = "FLASHWEAR Engagement"

    def ready(self):
        # Importing registers the order_paid / order_cancelled receivers.
        from apps.engagement import receivers  # noqa: F401
