"""Notification delivery selectors for the Back Office (Phase 17 §22).

What a screen may contain is decided here, not in the template:

* the list returns the inspection fields from the spec -- type, recipient, channel,
  status, created, scheduled, sent, failure reason (plus attempts) -- and deliberately
  **not** the body or metadata; content lives on :func:`notification_detail`, which only
  the detail route renders, so an export or a partial template cannot leak message
  content into a surface built for numbers;
* filters are allowlisted (status, channel, type come from the model's own choices) and
  sorting uses the shared :func:`sort_queryset` contract, so ``?filter=`` can only ever
  select a known state;
* :func:`notification_metrics` aggregates the spec's operational counts (§23) in one
  query: queued, sent, failed, unread, email failures and rows that needed a retry.
"""

from __future__ import annotations

from django.db.models import Count, Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.backoffice.selectors.common import sort_queryset
from apps.notifications.models import Channel, Notification, NotificationStatus, NotificationType

__all__ = [
    "CHANNEL_CHOICES",
    "NOTIFICATION_SORTS",
    "STATUS_CHOICES",
    "TYPE_CHOICES",
    "notification_detail",
    "notification_metrics",
    "notifications",
]

STATUS_CHOICES = (
    (NotificationStatus.PENDING, _("Scheduled")),
    (NotificationStatus.QUEUED, _("Queued")),
    (NotificationStatus.SENT, _("Sent")),
    (NotificationStatus.FAILED, _("Failed")),
    (NotificationStatus.CANCELLED, _("Cancelled")),
)

CHANNEL_CHOICES = tuple(Channel.choices)

#: Notification types for the filter menu: the enum's own choices, so the menu cannot
#: drift from what the registry can actually store.
TYPE_CHOICES = tuple(NotificationType.choices)

NOTIFICATION_SORTS: dict[str, list[str]] = {
    "-created_at": ["-created_at"],
    "created_at": ["created_at"],
    "-sent_at": ["-sent_at", "-created_at"],
    "status": ["status", "-created_at"],
    "channel": ["channel", "-created_at"],
    "notification_type": ["notification_type", "-created_at"],
}


def notifications(
    *,
    status: str = "",
    channel: str = "",
    notification_type: str = "",
    failed_only: bool = False,
    sort: str = "",
) -> QuerySet:
    """The inspection rows, newest first. No bodies, no metadata -- by design."""
    queryset = Notification.objects.values(
        "id",
        "notification_type",
        "category",
        "channel",
        "status",
        "attempts",
        "priority",
        "created_at",
        "scheduled_for",
        "sent_at",
        "failed_at",
        "failure_reason",
        "action_url",
        "user_id",
        "user__email",
    )
    if status in NotificationStatus.values:
        queryset = queryset.filter(status=status)
    if channel in Channel.values:
        queryset = queryset.filter(channel=channel)
    if notification_type in NotificationType.values:
        queryset = queryset.filter(notification_type=notification_type)
    if failed_only:
        queryset = queryset.filter(status=NotificationStatus.FAILED)
    queryset, _key = sort_queryset(queryset, sort, NOTIFICATION_SORTS, "-created_at")
    return queryset


def notification_detail(pk: int) -> Notification | None:
    """One row *with* content, for the detail route (gated by NOTIFICATIONS_VIEW)."""
    return Notification.objects.select_related("user").filter(pk=pk).first()


def notification_metrics() -> dict:
    """The spec's §23 counts in a single aggregate: no invented rates, only real states."""
    return Notification.objects.aggregate(
        queued=Count("id", filter=Q(status=NotificationStatus.QUEUED)),
        scheduled=Count("id", filter=Q(status=NotificationStatus.PENDING)),
        sent=Count("id", filter=Q(status=NotificationStatus.SENT)),
        failed=Count("id", filter=Q(status=NotificationStatus.FAILED)),
        unread=Count("id", filter=Q(channel=Channel.IN_APP, read_at__isnull=True)),
        email_failures=Count(
            "id",
            filter=Q(channel=Channel.EMAIL, status=NotificationStatus.FAILED),
        ),
        retried=Count("id", filter=Q(attempts__gt=1)),
        total=Count("id"),
    )
