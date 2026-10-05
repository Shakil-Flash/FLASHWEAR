"""Centralized notification and customer communication domain (Phase 17).

A cross-cutting layer, not a business domain: it owns delivery of messages other domains
decide to send (orders, payments, support, loyalty, drops, loop, quests) and never decides
*that* something happened -- that stays with the service that transitioned the state.
"""

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    verbose_name = "FLASH Notifications"
