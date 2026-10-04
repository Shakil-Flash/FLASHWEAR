"""FLASH Support & Customer Care (Phase 15): tickets, messages and the staff desk.

A leaf app in the migration graph: it references orders, payments, the catalogue,
engagement, the loop and quests so a ticket can point at the row it is about, but
nothing above it imports it. Support never mutates those domains -- it records
conversation and hands an operator a reference, which is what keeps a help desk
from becoming a second way to refund, cancel or restock.
"""

from django.apps import AppConfig


class SupportConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.support"
    verbose_name = "FLASH Support"
