"""Read-side selectors for the notification center and the API (Phase 17 §29).

Thin by design: filters and ordering in one place so the center page, the JSON API and
any future widget cannot disagree about what "my notifications" means. Two rules are
enforced *in the query*, never as a post-fetch check:

* **ownership** -- ``user=user``, so probing somebody else's id is indistinguishable from
  a miss (404, no oracle);
* **channel** -- the center shows ``in_app`` rows (the ones with a read state a customer
  can act on); ``email`` rows are delivery records for the back office, and listing them
  would double every fan-out event in the customer's list.
"""

from __future__ import annotations

from django.db.models import Count, Q, QuerySet

from apps.notifications.models import Channel, Notification

__all__ = [
    "status_counts",
    "user_notification",
    "user_notifications",
]


def user_notifications(
    user,
    *,
    unread: bool = False,
    category: str = "",
    notification_type: str = "",
    dismissed: bool = False,
) -> QuerySet:
    """The caller's in-app notifications, newest first, with optional filters.

    By default filters to active (undismissed) notifications.
    """
    qs = Notification.objects.filter(user=user, channel=Channel.IN_APP)
    if dismissed:
        qs = qs.filter(dismissed_at__isnull=False)
    else:
        qs = qs.filter(dismissed_at__isnull=True)

    if unread:
        qs = qs.filter(read_at__isnull=True)
    if category:
        qs = qs.filter(category=category)
    if notification_type:
        qs = qs.filter(notification_type=notification_type)
    return qs


def user_notification(user, pk: int) -> Notification | None:
    """One of the caller's in-app notifications, or None (detail view decides on 404)."""
    return (
        Notification.objects.filter(user=user, pk=pk, channel=Channel.IN_APP)
        .select_related("user")
        .first()
    )


def status_counts(user, *, include_archived: bool = False) -> dict[str, int]:
    """Unread/total counters for the center header and the bell badge.

    One aggregate instead of multiple counts: unread active, total active, and optionally archived.
    """
    row = Notification.objects.filter(user=user, channel=Channel.IN_APP).aggregate(
        unread=Count("id", filter=Q(read_at__isnull=True, dismissed_at__isnull=True)),
        total=Count("id", filter=Q(dismissed_at__isnull=True)),
        archived=Count("id", filter=Q(dismissed_at__isnull=False)),
    )
    res = {
        "unread": row["unread"] or 0,
        "total": row["total"] or 0,
    }
    if include_archived:
        res["archived"] = row["archived"] or 0
    return res

