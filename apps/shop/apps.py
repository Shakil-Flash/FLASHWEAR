"""FLASHWEAR shopping cart and wishlist.

This app owns the transactional layer that sits on top of the catalogue.
It knows nothing about inventory, payments, or fulfilment -- those belong
to Phase 6 and beyond.
"""

from __future__ import annotations

from django.apps import AppConfig


class ShopConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.shop"
    verbose_name = "FLASHWEAR Shop"

    def ready(self):
        # Importing registers the user_logged_in receiver that merges guest carts.
        from apps.shop import signals  # noqa: F401
