"""Celery delivery layer (Phase 17 §19-§21).

The boundary between "the platform recorded a notification" and "a provider accepted the
message". Three properties, in priority order:

1. **Never raise.** These tasks run from ``transaction.on_commit`` callbacks and are eager
   in tests (``CELERY_TASK_EAGER_PROPAGATES=True``) -- an exception here would surface in
   a caller's post-commit hook and can roll back unrelated work. Every failure is caught,
   logged, recorded on the row (or in the return value) and turned into a bounded retry.
2. **Idempotent under replay.** The sender claims the row with a compare-and-swap on
   ``(status, attempts)``: a redelivered queue message, a stuck-worker sweeper re-queue and
   the original delivery race to exactly one send. A row that is no longer ``queued`` is a
   no-op -- that is what makes re-running a batch harmless.
3. **Bounded retries.** Transient failures (SMTP hiccup, rate limit) retry with
   exponential backoff up to ``NOTIFICATIONS_MAX_EMAIL_ATTEMPTS``, then fail the row for
   the back office to inspect. Permanent failures (bad address, missing template, unknown
   provider) fail immediately -- retrying a deploy bug just burns the day's attempts.

In eager mode a transient failure retries synchronously inside the task (bounded by the
same attempt cap) instead of scheduling recursion, so tests exercise the real retry logic.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

from apps.notifications.models import Channel, Notification, NotificationStatus

logger = logging.getLogger("flashwear.notifications.tasks")

__all__ = [
    "broadcast_batch",
    "send_email",
    "sweep_drop_events",
    "sweep_points_expiring",
    "sweep_queue",
    "sweep_retention",
]


def _max_attempts() -> int:
    return max(1, int(getattr(settings, "NOTIFICATIONS_MAX_EMAIL_ATTEMPTS", 3)))


def _eager() -> bool:
    return bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False))


def _countdown(attempts: int) -> int:
    """Exponential backoff from the configured base, capped so a recovery is timely."""
    base = int(getattr(settings, "NOTIFICATIONS_RETRY_BACKOFF_SECONDS", 60))
    cap = int(getattr(settings, "NOTIFICATIONS_RETRY_MAX_SECONDS", 3600))
    return min(base * (2 ** max(0, attempts - 1)), cap)


def _fail(pk: int, reason: str) -> None:
    """CAS ``queued -> failed``; only the owner of the queue state may fail a row."""
    now = timezone.now()
    Notification.objects.filter(pk=pk, status=NotificationStatus.QUEUED).update(
        status=NotificationStatus.FAILED,
        failed_at=now,
        failure_reason=(reason or "Delivery failed.")[:300],
        updated_at=now,
    )


def _mark_sent(pk: int) -> None:
    now = timezone.now()
    Notification.objects.filter(pk=pk, status=NotificationStatus.QUEUED).update(
        status=NotificationStatus.SENT,
        sent_at=now,
        failure_reason="",
        updated_at=now,
    )


def _claim(pk: int) -> int | None:
    """CAS-claim one attempt. Returns the new attempt number, or None if not ours.

    Filtering on the *read* ``attempts`` value means two workers that both read ``0``
    cannot both claim: the second UPDATE matches zero rows. That is the whole duplicate
    protection -- no advisory locks, no SELECT FOR UPDATE required.
    """
    row = Notification.objects.filter(pk=pk, channel="email").values("status", "attempts").first()
    if row is None:
        return None
    if row["status"] != NotificationStatus.QUEUED:
        return None  # already sent, failed, cancelled or claimed elsewhere: replay no-op
    next_attempt = row["attempts"] + 1
    claimed = Notification.objects.filter(
        pk=pk, status=NotificationStatus.QUEUED, attempts=row["attempts"]
    ).update(attempts=next_attempt, updated_at=timezone.now())
    return next_attempt if claimed else None


def _reschedule(pk: int, countdown: int) -> None:
    try:
        send_email.apply_async(args=[pk], countdown=countdown)
    except Exception:  # pragma: no cover - broker outage must not become an exception
        logger.exception("Could not re-queue notification email %s", pk)


def _send_once(pk: int) -> str:
    """One render+send attempt. Returns ``sent`` or raises for the caller to classify."""
    from apps.notifications.services.email import get_email_provider
    from apps.notifications.services.templates import render_email

    row = Notification.objects.select_related("user").filter(pk=pk).first()
    if row is None:
        return "missing"
    subject, text, html = render_email(row)
    get_email_provider().send(
        subject=subject,
        text_body=text,
        html_body=html,
        recipient=row.user.email,
    )
    return "sent"


@shared_task(name="notifications.send_email")
def send_email(notification_id: int) -> dict:
    """Deliver one queued email row. Safe to call twice; the second call is a no-op."""
    from django.template import TemplateDoesNotExist, TemplateSyntaxError

    from apps.notifications.services.email import PermanentEmailError

    try:
        while True:
            attempt = _claim(notification_id)
            if attempt is None:
                row = Notification.objects.filter(pk=notification_id).values("status").first()
                return {"status": row["status"] if row else "missing", "skipped": True}
            try:
                outcome = _send_once(notification_id)
                if outcome == "sent":
                    _mark_sent(notification_id)
                    return {"status": "sent", "attempts": attempt}
                return {"status": "missing", "attempts": attempt}
            except PermanentEmailError as exc:
                _fail(notification_id, str(exc) or "Recipient rejected.")
                return {"status": "failed", "attempts": attempt, "permanent": True}
            except (TemplateDoesNotExist, TemplateSyntaxError) as exc:
                # Missing/broken templates cannot heal by retrying (deploy bug).
                _fail(notification_id, f"Template error: {exc}")
                return {"status": "failed", "attempts": attempt, "permanent": True}
            except Exception as exc:  # transient: provider, network, config
                if attempt >= _max_attempts():
                    _fail(notification_id, str(exc) or exc.__class__.__name__)
                    return {
                        "status": "failed",
                        "attempts": attempt,
                        "permanent": False,
                    }
                if _eager():
                    continue  # bounded synchronous retry; no scheduling recursion
                _reschedule(notification_id, _countdown(attempt))
                return {"status": "retrying", "attempts": attempt}
    except Exception:  # pragma: no cover - last-resort guard: this task must never raise
        logger.exception("Unexpected failure delivering notification %s", notification_id)
        return {"status": "error"}


# =============================================================================
# Sweepers
# =============================================================================


def _emit_new(*, user, idempotency_key: str, notification_type: str, **kwargs) -> int:
    """Emit one event unless this user already has a row for the key; return 0 or 1.

    ``emit`` returns the rows *standing* (deduplicated ones included), which is exactly
    right for callers proving idempotency -- but wrong for beat metrics: a sweep that
    counted standing rows would report yesterday's announcements again forever. The
    exists-check narrows the window, and the dispatcher's own key dedup closes it.
    """
    from apps.notifications.services.events import emit

    if Notification.objects.filter(user=user, idempotency_key=idempotency_key).exists():
        return 0
    emit(
        notification_type=notification_type,
        user=user,
        idempotency_key=idempotency_key,
        **kwargs,
    )
    return 1


def _queue_scheduled() -> int:
    """Promote due ``pending`` rows to their channel's active state."""
    now = timezone.now()
    due_ids = list(
        Notification.objects.filter(status=NotificationStatus.PENDING)
        .filter(Q(scheduled_for__lte=now) | Q(scheduled_for__isnull=True))
        .values_list("pk", "channel")
    )
    promoted = 0
    for pk, channel in due_ids:
        if channel == Channel.IN_APP:
            updated = Notification.objects.filter(pk=pk, status=NotificationStatus.PENDING).update(
                status=NotificationStatus.SENT, sent_at=now, updated_at=now
            )
        else:
            updated = Notification.objects.filter(pk=pk, status=NotificationStatus.PENDING).update(
                status=NotificationStatus.QUEUED, updated_at=now
            )
            if updated:
                _reschedule(pk, 0)
        promoted += int(bool(updated))
    return promoted


def _requeue_stuck() -> int:
    """Re-queue rows a dead worker took down while they were ``queued``.

    The age gate (``updated_at`` older than ``NOTIFICATIONS_STUCK_SECONDS``) is what keeps
    this from double-sending rows whose original task is simply still running.
    """
    now = timezone.now()
    stuck_before = now - timezone.timedelta(
        seconds=int(getattr(settings, "NOTIFICATIONS_STUCK_SECONDS", 600))
    )
    stuck_ids = list(
        Notification.objects.filter(
            channel=Channel.EMAIL,
            status=NotificationStatus.QUEUED,
            attempts__lt=_max_attempts(),
            updated_at__lt=stuck_before,
        ).values_list("pk", flat=True)
    )
    for pk in stuck_ids:
        _reschedule(pk, 0)
    return len(stuck_ids)


@shared_task(name="notifications.sweep_queue")
def sweep_queue() -> dict:
    """Beat task: promote due scheduled sends and recover stuck queue rows."""
    try:
        return {"promoted": _queue_scheduled(), "resent": _requeue_stuck()}
    except Exception:  # pragma: no cover - beat tasks must never raise
        logger.exception("notifications.sweep_queue failed")
        return {"promoted": 0, "resent": 0}


@shared_task(name="notifications.sweep_retention")
def sweep_retention() -> dict:
    """Beat task: honour the retention windows (Phase 17 §24).

    * in-app rows the customer has already read, older than ``NOTIFICATIONS_RETENTION_DAYS``
      -- unread rows are never deleted: silently dropping something the customer has not
      seen is losing their notification, not tidying up;
    * terminal email rows older than ``NOTIFICATIONS_EMAIL_RETENTION_DAYS`` -- the delivery
      record outlives the inbox copy so support can still audit what was sent last year.
    """
    try:
        now = timezone.now()
        in_app_cutoff = now - timezone.timedelta(
            days=int(getattr(settings, "NOTIFICATIONS_RETENTION_DAYS", 90))
        )
        email_cutoff = now - timezone.timedelta(
            days=int(getattr(settings, "NOTIFICATIONS_EMAIL_RETENTION_DAYS", 365))
        )
        deleted_in_app, _ = Notification.objects.filter(
            channel=Channel.IN_APP,
            read_at__isnull=False,
            read_at__lt=in_app_cutoff,
        ).delete()
        deleted_email, _ = Notification.objects.filter(
            channel=Channel.EMAIL,
            status__in=[
                NotificationStatus.SENT,
                NotificationStatus.FAILED,
                NotificationStatus.CANCELLED,
            ],
            created_at__lt=email_cutoff,
        ).delete()
        return {"in_app": deleted_in_app, "email": deleted_email}
    except Exception:  # pragma: no cover
        logger.exception("notifications.sweep_retention failed")
        return {"in_app": 0, "email": 0}


@shared_task(name="notifications.sweep_drop_events")
def sweep_drop_events() -> dict:
    """Beat task: time-based FLASH Drop announcements (Phase 17 §23).

    Reads ``NotificationSubscription`` rows (interest registered in the drop app) against
    the clock -- deliberately *not* by mutating ``FlashDrop.status``: reading a drop's
    schedule must not rewrite the domain object, and the idempotency keys
    (``drop:<id>:live:<user>`` / ``drop:<id>:ended:<user>``) make re-running harmless.
    Subscriptions are consumed once the drop has ended, so a long-retired drop stops
    generating rows the moment retention clears them.
    """
    try:
        from apps.accounts.models import User
        from apps.drops.models import FlashDrop
        from apps.notifications.models import NotificationSubscription, NotificationType

        now = timezone.now()
        subs = list(
            NotificationSubscription.objects.filter(
                notification_type__in=[NotificationType.DROP_LIVE, NotificationType.DROP_ENDED]
            ).values_list("related_object_id", "user_id")
        )
        drop_ids = {drop_id for drop_id, _user_id in subs}
        if not drop_ids:
            return {"live": 0, "ended": 0, "subs_consumed": 0}

        users = {
            user.pk: user
            for user in User.objects.filter(pk__in={uid for _did, uid in subs}, is_active=True)
        }
        live = ended = 0
        finished: set[int] = set()
        for drop in FlashDrop.objects.filter(pk__in=drop_ids):
            subscribers = [users[uid] for did, uid in subs if did == drop.pk and uid in users]
            context = {"drop_name": drop.name, "starts_at": drop.starts_at}
            if drop.starts_at and drop.starts_at <= now:
                for subscriber in subscribers:
                    live += _emit_new(
                        user=subscriber,
                        notification_type=NotificationType.DROP_LIVE,
                        idempotency_key=f"drop:{drop.pk}:live:{subscriber.pk}",
                        context=context,
                        related_object_type="drop",
                        related_object_id=drop.pk,
                        action_url=f"/drops/{drop.slug}/",
                    )
            if drop.ends_at and drop.ends_at <= now:
                for subscriber in subscribers:
                    ended += _emit_new(
                        user=subscriber,
                        notification_type=NotificationType.DROP_ENDED,
                        idempotency_key=f"drop:{drop.pk}:ended:{subscriber.pk}",
                        context=context,
                        related_object_type="drop",
                        related_object_id=drop.pk,
                        action_url=f"/drops/{drop.slug}/",
                    )
                finished.add(drop.pk)
        consumed = 0
        if finished:
            consumed, _ = NotificationSubscription.objects.filter(
                related_object_type="drop", related_object_id__in=finished
            ).delete()
        return {"live": live, "ended": ended, "subs_consumed": consumed}
    except Exception:  # pragma: no cover - beat tasks must never raise
        logger.exception("notifications.sweep_drop_events failed")
        return {"live": 0, "ended": 0, "subs_consumed": 0}


@shared_task(name="notifications.sweep_points_expiring")
def sweep_points_expiring() -> dict:
    """Beat task: warn before FLASH Points vanish (Phase 17 §22).

    Buckets positive, dated earns expiring inside the warning window by
    ``(user, expiry date)`` and emits one idempotent reminder per bucket -- one "you have
    500 points expiring Friday", not one per transaction row. Already-expired rows are
    left to the engagement expiry sweeper.
    """
    try:
        from apps.accounts.models import User
        from apps.engagement.models import PointsTransaction
        from apps.notifications.models import NotificationType

        now = timezone.now()
        horizon = now + timezone.timedelta(
            days=int(getattr(settings, "NOTIFICATIONS_EXPIRING_WARNING_DAYS", 7))
        )
        rows = list(
            PointsTransaction.objects.filter(
                transaction_type__in=PointsTransaction.POSITIVE_TYPES,
                expires_at__gt=now,
                expires_at__lte=horizon,
            )
            .values("user_id", "expires_at__date")
            .annotate(total=Sum("amount"))
        )
        if not rows:
            return {"notified": 0}
        users = {
            user.pk: user
            for user in User.objects.filter(pk__in={row["user_id"] for row in rows}, is_active=True)
        }
        notified = 0
        for row in rows:
            user = users.get(row["user_id"])
            if user is None:
                continue
            date_key = row["expires_at__date"].isoformat()
            notified += _emit_new(
                user=user,
                notification_type=NotificationType.POINTS_EXPIRING,
                idempotency_key=f"points:expiring:{row['user_id']}:{date_key}",
                context={"points": row["total"], "expires_on": date_key},
                related_object_type="points",
                action_url="/account/loyalty/",
            )
        return {"notified": notified}
    except Exception:  # pragma: no cover
        logger.exception("notifications.sweep_points_expiring failed")
        return {"notified": 0}


@shared_task(name="notifications.broadcast_batch")
def broadcast_batch(
    user_ids,
    notification_type: str,
    idempotency_key_template: str,
    context=None,
    channels=None,
    action_url="",
) -> dict:
    """Async slice of a fan-out (Phase 17 §26). Returns ``{"count": n}``."""
    try:
        from apps.notifications.services.bulk import broadcast_batch as _run

        count = _run(
            user_ids=list(user_ids),
            notification_type=notification_type,
            idempotency_key_template=idempotency_key_template,
            context=context,
            channels=channels,
            action_url=action_url,
        )
        return {"count": count}
    except Exception:  # pragma: no cover - a bad payload must not kill a worker
        logger.exception("notifications.broadcast_batch failed for %s", notification_type)
        return {"count": 0}
