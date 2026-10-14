"""Staff in-app notification services and event dispatchers (Phase 63).

Integrates with apps.notifications to provide a secure, idempotent in-app
notification center for staff members regarding tasks and operational events.
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import (
    Channel,
    Notification,
    NotificationCategory,
    NotificationType,
    Priority,
)
from apps.notifications.services import in_app
from apps.staff.models import StaffTask

logger = logging.getLogger(__name__)
User = get_user_model()


def notify_staff_user(
    *,
    user: Any,
    notification_type: str,
    title: str,
    body: str,
    action_url: str = "",
    priority: str = Priority.NORMAL,
    idempotency_key: str = "",
    related_object_type: str = "",
    related_object_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> Notification | None:
    """Send an in-app notification to a staff user, enforcing idempotency and security."""
    if user is None or not getattr(user, "is_active", False):
        return None

    if idempotency_key:
        existing = Notification.objects.filter(
            user=user,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            return existing

    try:
        with transaction.atomic():
            notification = in_app.create(
                user=user,
                notification_type=notification_type,
                category=NotificationCategory.STAFF,
                title=title[:200],
                body=body,
                action_url=action_url[:300] if action_url else "",
                priority=priority,
                idempotency_key=idempotency_key[:140] if idempotency_key else "",
                related_object_type=related_object_type[:32] if related_object_type else "",
                related_object_id=related_object_id,
                metadata=metadata or {},
            )
            return notification
    except Exception as exc:  # Resilience: do not break caller transaction if notification fails
        logger.warning(
            "Failed to create staff notification for user %s: %s",
            getattr(user, "pk", "unknown"),
            exc,
            exc_info=True,
        )
        return None


def notify_task_assigned(*, task: StaffTask, assigned_by: Any) -> Notification | None:
    """Notify the assignee that a new task was assigned to them."""
    if not task.assignee:
        return None
    # Don't notify if the user assigned it to themselves
    if assigned_by and assigned_by.pk == task.assignee_id:
        return None

    idempotency_key = f"staff_task:{task.pk}:assigned:{task.assignee_id}"
    actor_label = assigned_by.get_full_name() or assigned_by.email if assigned_by else "A manager"
    title = f"Task #{task.pk} Assigned to You"
    body = (
        f"{actor_label} assigned you task '{task.title}' with "
        f"{task.get_priority_display()} priority."
    )

    return notify_staff_user(
        user=task.assignee,
        notification_type=NotificationType.STAFF_TASK_ASSIGNED,
        title=title,
        body=body,
        action_url=f"/staff/tasks/{task.pk}/",
        priority=Priority.HIGH if task.priority in {"high", "urgent"} else Priority.NORMAL,
        idempotency_key=idempotency_key,
        related_object_type="staff_task",
        related_object_id=task.pk,
        metadata={"task_id": task.pk, "priority": task.priority},
    )


def notify_task_reassigned(
    *, task: StaffTask, reassigned_by: Any, previous_assignee: Any
) -> Notification | None:
    """Notify the new assignee that a task was reassigned to them."""
    if not task.assignee:
        return None
    if reassigned_by and reassigned_by.pk == task.assignee_id:
        return None

    now_ts = int(timezone.now().timestamp())
    idempotency_key = f"staff_task:{task.pk}:reassigned:{task.assignee_id}:{now_ts}"
    actor_label = (
        reassigned_by.get_full_name() or reassigned_by.email if reassigned_by else "A manager"
    )
    prev_label = previous_assignee.email if previous_assignee else "unassigned"
    title = f"Task #{task.pk} Reassigned to You"
    body = f"{actor_label} reassigned task '{task.title}' to you (previously {prev_label})."

    return notify_staff_user(
        user=task.assignee,
        notification_type=NotificationType.STAFF_TASK_REASSIGNED,
        title=title,
        body=body,
        action_url=f"/staff/tasks/{task.pk}/",
        priority=Priority.HIGH if task.priority in {"high", "urgent"} else Priority.NORMAL,
        idempotency_key=idempotency_key,
        related_object_type="staff_task",
        related_object_id=task.pk,
        metadata={"task_id": task.pk, "priority": task.priority},
    )


def notify_task_status_changed(
    *,
    task: StaffTask,
    changed_by: Any,
    old_status: str,
    new_status: str,
) -> list[Notification]:
    """Notify task creator and/or assignee of status changes (e.g. blocked, completed)."""
    notifications: list[Notification] = []
    actor_label = changed_by.get_full_name() or changed_by.email if changed_by else "Staff"
    now_ts = int(timezone.now().timestamp())

    # Recipients: creator and assignee (if different from actor)
    recipients = []
    if task.creator and (not changed_by or changed_by.pk != task.creator_id):
        recipients.append(task.creator)
    if task.assignee and (not changed_by or changed_by.pk != task.assignee_id):
        if task.assignee not in recipients:
            recipients.append(task.assignee)

    title = f"Task #{task.pk} Status: {task.get_status_display()}"
    body = f"{actor_label} updated task '{task.title}' status from {old_status} to {new_status}."

    prio = Priority.HIGH if new_status in {"blocked", "completed"} else Priority.NORMAL

    for recipient in recipients:
        idempotency_key = f"staff_task:{task.pk}:status:{new_status}:{recipient.pk}:{now_ts}"
        notif = notify_staff_user(
            user=recipient,
            notification_type=NotificationType.STAFF_TASK_STATUS,
            title=title,
            body=body,
            action_url=f"/staff/tasks/{task.pk}/",
            priority=prio,
            idempotency_key=idempotency_key,
            related_object_type="staff_task",
            related_object_id=task.pk,
            metadata={"task_id": task.pk, "old_status": old_status, "new_status": new_status},
        )
        if notif:
            notifications.append(notif)

    return notifications


def notify_task_comment(
    *, task: StaffTask, author: Any, comment_snippet: str
) -> list[Notification]:
    """Notify task creator and assignee when a new operational comment is added."""
    notifications: list[Notification] = []
    author_label = author.get_full_name() or author.email if author else "A colleague"
    now_ts = int(timezone.now().timestamp())

    recipients = []
    if task.creator and (not author or author.pk != task.creator_id):
        recipients.append(task.creator)
    if task.assignee and (not author or author.pk != task.assignee_id):
        if task.assignee not in recipients:
            recipients.append(task.assignee)

    title = f"New Comment on Task #{task.pk}"
    snippet = comment_snippet[:100] + ("..." if len(comment_snippet) > 100 else "")
    body = f"{author_label} commented on '{task.title}': \"{snippet}\""

    for recipient in recipients:
        idempotency_key = (
            f"staff_task:{task.pk}:comment:{author.pk if author else 0}:{recipient.pk}:{now_ts}"
        )
        notif = notify_staff_user(
            user=recipient,
            notification_type=NotificationType.STAFF_TASK_COMMENT,
            title=title,
            body=body,
            action_url=f"/staff/tasks/{task.pk}/",
            priority=Priority.NORMAL,
            idempotency_key=idempotency_key,
            related_object_type="staff_task",
            related_object_id=task.pk,
            metadata={"task_id": task.pk},
        )
        if notif:
            notifications.append(notif)

    return notifications


def notify_task_deadline_reminder(*, task: StaffTask, kind: str) -> Notification | None:
    """Notify assignee about approaching deadline (due_soon) or missed deadline (overdue).

    Kind must be 'due_soon' or 'overdue'.
    """
    if not task.assignee or task.is_terminal or not task.due_date:
        return None

    date_str = task.due_date.strftime("%Y-%m-%d")
    idempotency_key = f"staff_task:{task.pk}:deadline:{kind}:{date_str}"

    if Notification.objects.filter(user=task.assignee, idempotency_key=idempotency_key).exists():
        return None

    if kind == "overdue":
        title = f"Task #{task.pk} is Overdue"
        due_str = task.due_date.strftime("%b %d, %Y %H:%M")
        body = f"Task '{task.title}' was due on {due_str}. Please take action."
        priority = Priority.HIGH
    else:
        title = f"Task #{task.pk} Deadline Approaching"
        body = f"Task '{task.title}' is due soon ({task.due_date.strftime('%b %d, %Y %H:%M')})."
        priority = Priority.NORMAL

    return notify_staff_user(
        user=task.assignee,
        notification_type=NotificationType.STAFF_TASK_DEADLINE,
        title=title,
        body=body,
        action_url=f"/staff/tasks/{task.pk}/",
        priority=priority,
        idempotency_key=idempotency_key,
        related_object_type="staff_task",
        related_object_id=task.pk,
        metadata={"task_id": task.pk, "kind": kind, "due_date": task.due_date.isoformat()},
    )


# --------------------------------------------------------------------------------------
# Staff Notification Inbox Queries & Actions
# --------------------------------------------------------------------------------------


def get_staff_notifications(user: Any, *, category: str | None = None, unread_only: bool = False):
    """Retrieve staff notifications for the authenticated user, newest first.

    Guarantees user isolation (ownership filter).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return Notification.objects.none()

    qs = Notification.objects.filter(
        user=user,
        channel=Channel.IN_APP,
        dismissed_at__isnull=True,
    ).order_by("-created_at")

    if category:
        qs = qs.filter(category=category)
    else:
        # Default to staff & operational category if unspecified
        qs = qs.filter(category=NotificationCategory.STAFF)

    if unread_only:
        qs = qs.filter(read_at__isnull=True)

    return qs


def get_staff_unread_count(user: Any) -> int:
    """Return count of unread staff notifications for user."""
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    return Notification.objects.filter(
        user=user,
        channel=Channel.IN_APP,
        category=NotificationCategory.STAFF,
        read_at__isnull=True,
        dismissed_at__isnull=True,
    ).count()


def mark_staff_notification_read(user: Any, pk: int) -> Notification | None:
    """Mark a single staff notification as read. Enforces user ownership."""
    return in_app.mark_read(user, pk)


def mark_all_staff_notifications_read(user: Any) -> int:
    """Mark all unread staff notifications as read for this user."""
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    now = timezone.now()
    return Notification.objects.filter(
        user=user,
        channel=Channel.IN_APP,
        category=NotificationCategory.STAFF,
        read_at__isnull=True,
        dismissed_at__isnull=True,
    ).update(read_at=now, updated_at=now)
