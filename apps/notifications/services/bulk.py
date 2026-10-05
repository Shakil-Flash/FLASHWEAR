"""Bulk fan-out (Phase 17 §26).

Campaign-shaped emission: one business event, many recipients. Two properties make it
safe to run unattended:

* **per-recipient idempotency** -- the key template must interpolate ``{user_id}``, so a
  replayed batch deduplicates against rows from the first run instead of double-posting;
* **per-recipient isolation** -- each emit goes through the never-raising
  :func:`apps.notifications.services.events.emit`, so one broken recipient (deactivated
  mid-batch, pathological context value) is logged and skipped, never a reason to abandon
  the other 499.

No campaign UI exists in this phase (there is no campaign model to hang one on); this is
the tested delivery engine behind ``notifications.broadcast_batch`` and any future
marketer's screen.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from django.conf import settings

from apps.notifications.services.events import emit

logger = logging.getLogger("flashwear.notifications")

__all__ = ["broadcast", "broadcast_batch"]


def _batch_size() -> int:
    return max(1, int(getattr(settings, "NOTIFICATIONS_BROADCAST_BATCH_SIZE", 500)))


def broadcast_batch(
    *,
    user_ids: Sequence[int],
    notification_type: str,
    idempotency_key_template: str,
    context: dict[str, Any] | None = None,
    channels: Iterable[str] | None = None,
    action_url: str = "",
) -> int:
    """Emit to this slice of recipients. The unit the Celery task schedules on."""
    if "{user_id}" not in idempotency_key_template:
        raise ValueError("idempotency_key_template must contain {user_id}.")

    from apps.accounts.models import User

    count = 0
    # Active users only, resolved in one query: deactivating an account between campaign
    # build and send must not produce rows (or emails) addressed to them.
    users = User.objects.filter(pk__in=list(user_ids), is_active=True)
    for user in users.iterator(chunk_size=_batch_size()):
        rows = emit(
            notification_type=notification_type,
            user=user,
            idempotency_key=idempotency_key_template.format(user_id=user.pk),
            context=context,
            channels=channels,
            action_url=action_url,
        )
        count += len(rows)
    return count


def broadcast(
    *,
    user_ids: Iterable[int],
    notification_type: str,
    idempotency_key_template: str,
    context: dict[str, Any] | None = None,
    channels: Iterable[str] | None = None,
    action_url: str = "",
    batch_size: int | None = None,
) -> int:
    """Fan out synchronously in slices of ``batch_size`` (default: the setting).

    Synchronous on purpose for small, testable fan-outs; large campaigns should enqueue
    ``notifications.broadcast_batch`` per slice instead of holding a request open.
    Returns the number of rows standing after all slices.
    """
    ids = [pk for pk in dict.fromkeys(user_ids) if pk]  # de-duplicated, order preserved
    size = batch_size or _batch_size()
    total = 0
    for start in range(0, len(ids), size):
        total += broadcast_batch(
            user_ids=ids[start : start + size],
            notification_type=notification_type,
            idempotency_key_template=idempotency_key_template,
            context=context,
            channels=channels,
            action_url=action_url,
        )
    return total
