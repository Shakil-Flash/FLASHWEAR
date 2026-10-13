"""Safe retry service for staff operations (Phase 51 §5, §7).

Guarantees:
- Only moves a row that is FAILED right now.
- Idempotent CAS update.
- Recipient must be active.
- If promotional, MUST verify current marketing consent/suppression (staff cannot bypass opt-outs).
- Channel must be supported and configured.
"""

from __future__ import annotations

import logging
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import (
    Channel,
    Notification,
    NotificationCategory,
    NotificationStatus,
    NotificationType,
)
from apps.notifications.services import preferences
from apps.notifications.services.push import is_push_configured
from apps.notifications.services.sms import is_sms_configured

logger = logging.getLogger("flashwear.notifications.retry")

__all__ = ["retry_failed_notification"]


def retry_failed_notification(notification_id: int) -> tuple[bool, str]:
    """Safely retry a failed notification row."""
    notif = Notification.objects.select_related("user").filter(pk=notification_id).first()
    if notif is None:
        return False, "Notification not found."

    if notif.status != NotificationStatus.FAILED:
        return False, f"Notification is not failed (current status: {notif.status})."

    user = notif.user
    if not user.is_active:
        return False, "Cannot retry: recipient account is inactive."

    # Check promotional consent immediately before retry
    is_promo = (
        notif.category == NotificationCategory.PROMOTIONS
        or notif.notification_type == NotificationType.PROMOTION_AVAILABLE
    )
    if is_promo:
        if notif.channel == Channel.EMAIL and not preferences.marketing_subscribed(user):
            return False, "Cannot retry: customer has opted out of marketing emails."
        elif notif.channel == Channel.SMS and not preferences.marketing_sms_subscribed(user):
            return False, "Cannot retry: customer has opted out of marketing SMS."
        elif notif.channel == Channel.PUSH and not preferences.marketing_push_subscribed(user):
            return False, "Cannot retry: customer has opted out of marketing push notifications."

    # Channel configuration check
    if notif.channel == Channel.SMS and not is_sms_configured():
        return False, "Cannot retry: SMS provider is not configured."
    if notif.channel == Channel.PUSH and not is_push_configured():
        return False, "Cannot retry: Push provider is not configured."
    if notif.channel == Channel.IN_APP:
        moved = Notification.objects.filter(
            pk=notification_id,
            status=NotificationStatus.FAILED,
        ).update(
            status=NotificationStatus.SENT,
            sent_at=timezone.now(),
            failure_reason="",
            failed_at=None,
            updated_at=timezone.now(),
        )
        return bool(moved), "In-app notification marked as delivered."

    with transaction.atomic():
        moved = Notification.objects.filter(
            pk=notification_id,
            status=NotificationStatus.FAILED,
        ).update(
            status=NotificationStatus.QUEUED,
            attempts=0,
            failure_reason="",
            failed_at=None,
            updated_at=timezone.now(),
        )
        if not moved:
            return False, "Notification was updated by another process."

        if notif.channel == Channel.EMAIL:
            from apps.notifications.tasks import send_email

            transaction.on_commit(lambda: send_email.delay(notification_id))
        elif notif.channel == Channel.SMS:
            from apps.notifications.tasks import send_sms

            transaction.on_commit(lambda: send_sms.delay(notification_id))
        elif notif.channel == Channel.PUSH:
            from apps.notifications.tasks import send_push

            transaction.on_commit(lambda: send_push.delay(notification_id))

    return True, "Notification re-queued for delivery."
