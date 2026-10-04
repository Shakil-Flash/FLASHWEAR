"""Drops app configuration."""

from django.apps import AppConfig


class DropsAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.drops"
    verbose_name = "FLASH Drops"
