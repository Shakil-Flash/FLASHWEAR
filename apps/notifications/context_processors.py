"""Context processor: the navbar bell badge (Phase 17 §29).

Mirrors the ``cart_summary`` contract: anonymous callers get an empty dict, and *any*
failure degrades to "no badge" rather than breaking every page render -- a notification
count is decoration, and an exception here would take down error pages too (this runs on
404/500 renders, where the database may be exactly what is wrong).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("flashwear.notifications")


def unread_count(request) -> dict:
    """Expose ``notifications_unread`` to every template."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return {"notifications_unread": 0}
    try:
        from apps.notifications.services.in_app import unread_count as count_unread

        return {"notifications_unread": count_unread(user)}
    except Exception:  # pragma: no cover - defensive by contract
        logger.exception("Could not count unread notifications for badge")
        return {"notifications_unread": 0}
