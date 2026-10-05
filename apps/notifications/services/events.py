"""Domain-facing emission (Phase 17 §19).

The API every *other* app calls. Two rules shape it:

1. **A notification never fails a workflow.** A checkout, a refund or a support reply
   must succeed even if the notification database write, a template or (later) the broker
   is broken -- so :func:`emit` catches everything, logs at exception level and returns an
   empty list. Unit tests that *want* the strict behaviour (unknown type raises) import
   :mod:`.dispatcher` directly.
2. **It runs inside the caller's transaction.** Rows are created atomically with the state
   change that caused them: if the order rolls back, its "order placed" notification rolls
   back too. Email sending is the exception, deferred to ``on_commit`` by the dispatcher.

Everything here is synchronous bookkeeping; actual delivery is the Celery layer in
:mod:`apps.notifications.tasks`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from django.utils import timezone

from apps.notifications.models import Channel, Notification, NotificationStatus
from apps.notifications.services.dispatcher import emit as _emit_strict

logger = logging.getLogger("flashwear.notifications")

__all__ = ["emit", "record_email_result"]


def emit(
    *,
    notification_type: str,
    user,
    idempotency_key: str,
    context: dict[str, Any] | None = None,
    channels: Iterable[str] | None = None,
    action_url: str = "",
    related_object_type: str = "",
    related_object_id: int | None = None,
    priority: str | None = None,
    scheduled_for=None,
) -> list[Notification]:
    """Safe wrapper around the dispatcher: log-and-continue, never raise."""
    try:
        return _emit_strict(
            notification_type=notification_type,
            user=user,
            idempotency_key=idempotency_key,
            context=context,
            channels=channels,
            action_url=action_url,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            priority=priority,
            scheduled_for=scheduled_for,
        )
    except Exception:
        logger.exception(
            "Notification emit failed (type=%s user=%s key=%r)",
            notification_type,
            getattr(user, "pk", None),
            idempotency_key,
        )
        return []


def record_email_result(
    *,
    notification_type: str,
    user,
    idempotency_key: str,
    accepted: bool,
    context: dict[str, Any] | None = None,
    action_url: str = "",
    related_object_type: str = "",
    related_object_id: int | None = None,
    failure_reason: str = "",
) -> Notification | None:
    """Log an email that was sent (or failed) by a path outside this dispatcher.

    Phase 17 keeps the Phase 15 support mailer exactly as it is -- its outbox tests pin
    that behaviour -- but the back office still needs one delivery table to look at. So
    the legacy sender reports its outcome here as an already-terminal row: ``sent`` when
    the backend accepted it, ``failed`` when it did not, with no task and no queue.

    Never raises, same contract as :func:`emit`.
    """
    try:
        spec_context = dict(context or {})
        from apps.notifications.services import preferences, templates

        spec = templates.get_spec(notification_type)
        if user is None or not getattr(user, "is_active", False):
            return None
        if not preferences.user_can_receive_email(user):
            return None
        title, body = templates.render_notification(spec, spec_context)
        from apps.notifications.services.dispatcher import _json_safe, _validate_action_url

        now = timezone.now()
        defaults = {
            "notification_type": notification_type,
            "category": spec.category,
            "title": title,
            "body": body,
            "status": (NotificationStatus.SENT if accepted else NotificationStatus.FAILED),
            "priority": spec.priority,
            "action_url": _validate_action_url(action_url),
            "metadata": _json_safe(spec_context),
            "related_object_type": related_object_type,
            "related_object_id": related_object_id,
            "sent_at": now if accepted else None,
            "failed_at": None if accepted else now,
            "failure_reason": "" if accepted else (failure_reason or "Delivery failed.")[:300],
            "attempts": 1,
        }
        if not idempotency_key:
            return Notification.objects.create(user=user, channel=Channel.EMAIL, **defaults)
        row, _created = Notification.objects.get_or_create(
            user=user,
            idempotency_key=idempotency_key,
            channel=Channel.EMAIL,
            defaults=defaults,
        )
        return row
    except Exception:
        logger.exception(
            "Recording email result failed (type=%s key=%r)",
            notification_type,
            idempotency_key,
        )
        return None
