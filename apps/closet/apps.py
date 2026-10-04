"""FLASHWEAR closet: the digital wardrobe and outfit builder (Phase 8).

A leaf app like ``engagement``: it reads the catalogue and orders through relationships,
while nothing above imports it -- the account area wires its URLs and pages reach it only
through its own services. There are no receivers on purpose: nothing is inserted into the
wardrobe automatically, so order webhooks and replays never touch this app.
"""

from __future__ import annotations

from django.apps import AppConfig


class ClosetConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.closet"
    verbose_name = "FLASHWEAR Closet"
