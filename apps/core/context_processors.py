"""Template context processors."""

from __future__ import annotations

import logging

from django.db import DatabaseError, ProgrammingError
from django.http import HttpRequest

from apps.core.models import SiteConfiguration

logger = logging.getLogger("flashwear.core")


def get_site_configuration() -> SiteConfiguration:
    """Return the singleton configuration, degrading gracefully.

    The context processor runs on *every* template render, including error pages. If the
    database is unavailable we must not raise (that would turn a 500 into a crash loop),
    so fall back to an unsaved instance carrying the model defaults.
    """
    try:
        config = SiteConfiguration.load()
        if config is not None:
            return config
    except (DatabaseError, ProgrammingError):
        logger.warning("Site configuration unavailable; using model defaults.")
        return SiteConfiguration()

    # The row is seeded by migration 0002. If it is missing (e.g. a test using an empty
    # database) fall back to unsaved defaults rather than writing during a render.
    logger.warning("Site configuration row is missing; using model defaults.")
    return SiteConfiguration()


def site(request: HttpRequest) -> dict:
    """Expose global site configuration to every template."""
    config = get_site_configuration()
    return {
        "site": config,
        "site_name": config.site_name,
        "site_tagline": config.tagline,
        "site_announcement": config.announcement,
        "site_maintenance_mode": config.maintenance_mode,
        "seo_title": config.default_seo_title,
        "seo_description": config.default_seo_description,
        "support_email": config.support_email,
    }
