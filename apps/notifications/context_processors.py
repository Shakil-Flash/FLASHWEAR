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
    """Expose ``notifications_unread`` to every template.

    Two refinements keep the badge off the hot path: a view that already computed the
    count (the notification centre) stashes it on the request so this runs once per
    request rather than twice, and the back-office -- whose chrome has no bell -- is
    skipped entirely.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return {"notifications_unread": 0}
    match = getattr(request, "resolver_match", None)
    if match is not None and "backoffice" in match.app_names:
        return {"notifications_unread": 0}
    precomputed = getattr(request, "_fw_unread_count", None)
    if precomputed is not None:
        return {"notifications_unread": precomputed}
    try:
        from apps.notifications.services.in_app import unread_count as count_unread

        count = count_unread(user)
        try:
            request._fw_unread_count = count
        except AttributeError:  # pragma: no cover - synthesised requests
            pass
        return {"notifications_unread": count}
    except Exception:  # pragma: no cover - defensive by contract
        logger.exception("Could not count unread notifications for badge")
        return {"notifications_unread": 0}
