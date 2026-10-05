"""The dispatcher (Phase 17 §10, §19): one funnel every notification goes through.

Order of operations, each step earning its place:

1. **Validate** -- an unknown type is a bug, so it raises here (``events.emit`` turns it
   into a log line for domain callers); an invalid action URL is refused before it can
   become an open redirect.
2. **Resolve channels** -- registry declares what a type *wants*; :mod:`.preferences`
   decides what this recipient *gets* (mandatory categories, preference rows, marketing
   opt-in, usable address).
3. **Render once** -- titles/bodies render before any row exists, so a broken template
   fails the whole emit cleanly rather than leaving half a fan-out behind.
4. **Create idempotently** -- inside one atomic block, per channel:
   ``get_or_create(user, idempotency_key, channel)`` with a savepoint plus an
   ``IntegrityError`` fallback, so two concurrent dispatches of the same business event
   race into exactly one row each (the DB constraint from §32 is the real guarantee; the
   Python-level fallback just picks the winner).
5. **Queue email via ``transaction.on_commit``** -- the sender task must never see a row
   its transaction has not committed yet, and in tests built on transactional fixtures
   the callback simply never fires (which is why no existing outbox assertion can be
   disturbed by an emit).

In-app rows are created ``sent`` (their presence *is* the delivery); email rows are
created ``queued`` and handed to :mod:`apps.notifications.tasks`. Scheduled sends stay
``pending`` until the queue sweeper promotes them.
"""

from __future__ import annotations

import decimal
import logging
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.notifications.models import (
    Channel,
    Notification,
    NotificationStatus,
)
from apps.notifications.services import preferences, templates

logger = logging.getLogger("flashwear.notifications.dispatcher")

__all__ = ["emit"]

_ACTION_URL_BAD_PREFIXES = ("http://", "https://", "//", "mailto:", "javascript:")


def _validate_action_url(action_url: str) -> str:
    """Internal links only: a notification must never become an open redirect.

    The model's validator only runs on ``full_clean`` (admin/forms), so the dispatcher
    enforces the same rule at the trust boundary where domain data enters.
    """
    action_url = (action_url or "").strip()
    if not action_url:
        return ""
    if not action_url.startswith("/") or action_url.lower().startswith(_ACTION_URL_BAD_PREFIXES):
        raise ValueError(f"action_url must be a site-relative path, got {action_url!r}")
    return action_url


def _json_safe(value: Any) -> Any:
    """Coerce context values to what a JSONField can store, without losing display data.

    Domains hand over Decimals, lazy translations, datetimes -- all legal template
    context but illegal JSON. Money and dates become their display strings (that is what
    the re-render shows anyway); anything unrecognised degrades to ``str`` rather than
    exploding the dispatch.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _queue_email(pk: int):
    """An ``on_commit`` callback: schedule the sender without ever raising.

    Whatever happens (broker down, bad pk, worker misconfigured) it is logged, never
    raised -- raising here would surface *after* commit, in code the caller has already
    left, and could roll back an unrelated outer operation in eager mode.
    """

    def _schedule() -> None:
        try:
            from apps.notifications.tasks import send_email

            send_email.delay(pk)
        except Exception:  # pragma: no cover - defensive by design
            logger.exception("Failed to queue notification email %s", pk)

    return _schedule


def _create_row(
    *,
    user,
    notification_type: str,
    channel: str,
    category: str,
    title: str,
    body: str,
    status: str,
    priority: str,
    idempotency_key: str,
    action_url: str,
    metadata: dict[str, Any],
    related_object_type: str,
    related_object_id: int | None,
    scheduled_for,
) -> tuple[Notification, bool]:
    """Create-or-fetch one row for one channel (Phase 17 §32)."""
    defaults = {
        "notification_type": notification_type,
        "category": category,
        "title": title,
        "body": body,
        "status": status,
        "priority": priority,
        "action_url": action_url,
        "metadata": metadata,
        "related_object_type": related_object_type,
        "related_object_id": related_object_id,
        "scheduled_for": scheduled_for,
    }
    if channel == Channel.IN_APP and status == NotificationStatus.SENT:
        now = timezone.now()
        defaults["sent_at"] = now

    if not idempotency_key:
        # No key means "not deduplicatable" -- never filter on the empty string, or the
        # first unrelated row for this user+channel would be returned as "existing".
        return Notification.objects.create(user=user, channel=channel, **defaults), True

    try:
        with transaction.atomic():
            return Notification.objects.get_or_create(
                user=user,
                idempotency_key=idempotency_key,
                channel=channel,
                defaults=defaults,
            )
    except IntegrityError:
        # Lost a race with a concurrent dispatcher: adopt the winner's row.
        return (
            Notification.objects.get(user=user, idempotency_key=idempotency_key, channel=channel),
            False,
        )


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
    """Dispatch one business event. Returns the rows now standing (deduplicated ones
    included -- they are the proof the emit was idempotent).

    Raises ``ValueError`` for unknown types or bad action URLs; every *domain* caller
    goes through :func:`apps.notifications.services.events.emit`, which logs instead of
    raising, because a notification must never fail the workflow that triggered it.
    """
    try:
        spec = templates.get_spec(notification_type)
    except KeyError as exc:
        raise ValueError(f"Unknown notification type: {notification_type!r}") from exc

    if user is None or not getattr(user, "is_active", False):
        return []

    action_url = _validate_action_url(action_url)
    context = dict(context or {})
    allowed = preferences.effective_channels(user, notification_type, channels)
    if not allowed:
        return []

    title, body = templates.render_notification(spec, context)
    metadata = _json_safe(context)
    due = scheduled_for is None or scheduled_for <= timezone.now()

    created: list[Notification] = []
    with transaction.atomic():
        for channel in allowed:
            if channel == Channel.IN_APP:
                row_status = NotificationStatus.PENDING if not due else NotificationStatus.SENT
            else:
                row_status = NotificationStatus.PENDING if not due else NotificationStatus.QUEUED
            row, was_created = _create_row(
                user=user,
                notification_type=notification_type,
                channel=channel,
                category=spec.category,
                title=title,
                body=body,
                status=row_status,
                priority=priority or spec.priority,
                idempotency_key=idempotency_key,
                action_url=action_url,
                metadata=metadata,
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                scheduled_for=scheduled_for,
            )
            created.append(row)
            if was_created and channel == Channel.EMAIL and row_status == NotificationStatus.QUEUED:
                # Only *after* the row exists, *inside* the same atomic block: the task
                # fires once this transaction commits, never before.
                transaction.on_commit(_queue_email(row.pk))
    return created
