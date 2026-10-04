"""FLASH Quests & Rewards (Phase 14): deterministic quests, progress and badges.

A leaf app in the migration graph: it reads orders, engagement, closet, styling,
shop, drops, loop and (optionally) creator rows to derive progress, and it writes
only into its own tables plus idempotent FLASH Points ``BONUS`` ledger entries.
Nothing above it imports it, so the gamification layer stays additive.
"""

from django.apps import AppConfig


class QuestsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.quests"
    verbose_name = "FLASH Quests"

    def ready(self):
        # Importing registers the order_paid receiver that feeds quest progress.
        from apps.quests import signals  # noqa: F401
