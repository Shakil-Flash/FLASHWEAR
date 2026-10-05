"""In-app delivery (Phase 17 §11).

In-app is the one channel with no provider and no failure mode outside the database: a
row with ``status=sent`` *is* the delivery -- the moment :func:`mark_read` runs, the
customer has seen it. So this module owns creation and read-state only; scheduling,
idempotency and preferences all live in the dispatcher, which every channel shares.
"""

from __future__ import annotations

from django.utils import timezone

from apps.notifications.models import Channel, Notification, NotificationStatus

__all__ = [
    "create",
    "mark_all_read",
    "mark_read",
    "unread_count",
    "unread_ids",
]


def create(**kwargs) -> Notification:
    """Create an in-app row already in ``sent`` state.

    The row's arrival *is* the delivery: there is no queue to wait on, which is why the
    dispatcher sets ``sent_at`` at creation rather than pretending a worker picked it up.
    """
    notification = Notification(channel=Channel.IN_APP, **kwargs)
    notification.status = NotificationStatus.SENT
    notification.sent_at = timezone.now()
    notification.save()
    return notification


def mark_read(user, pk: int) -> Notification | None:
    """Mark one of *this* caller's notifications read. Returns None when it is not theirs.

    Ownership is enforced by filtering on ``user`` rather than fetching-then-checking:
    there is no window in which another recipient's row could be touched, and a probe for
    someone else's id answers exactly like a row that does not exist (404, no oracle).
    """
    notification = Notification.objects.filter(user=user, pk=pk).first()
    if notification is None:
        return None
    return notification.mark_read()


def mark_all_read(user) -> int:
    """Mark every unread in-app row read. Idempotent; returns how many rows changed.

    Scoped to ``IN_APP``: an email delivery row is an operational record (what the back
    office audits), not something sitting in this customer's center waiting to be seen --
    counting it would double every event that fans out to both channels.
    """
    now = timezone.now()
    return Notification.objects.filter(
        user=user, channel=Channel.IN_APP, read_at__isnull=True
    ).update(read_at=now, updated_at=now)


def unread_count(user) -> int:
    """The bell badge: unread in-app rows only (what the center actually lists).

    Cheap (index on ``user`` + ``read_at``) and safe for anonymous callers to never
    reach -- views pass ``request.user`` only when authenticated.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    return Notification.objects.filter(
        user=user, channel=Channel.IN_APP, read_at__isnull=True
    ).count()


def unread_ids(user, limit: int = 200) -> list[int]:
    """Unread in-app ids, newest first. Used by "mark these visible rows read" updates."""
    return list(
        Notification.objects.filter(user=user, channel=Channel.IN_APP, read_at__isnull=True)
        .order_by("-created_at")
        .values_list("id", flat=True)[:limit]
    )
