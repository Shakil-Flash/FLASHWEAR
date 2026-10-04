"""Celery tasks for engagement (Phase 7).

One sweeper, mirroring ``inventory.sweep_expired_reservations``: it releases checkout-only
points holds that outlived their timer and writes expiry rows for earns past their date. Both
halves are idempotent, so beat firing twice (or a redelivery) is harmless.

The schedule lives in ``CELERY_BEAT_SCHEDULE`` against ``LOYALTY_SWEEP_INTERVAL_SECONDS``.
"""

from __future__ import annotations

from celery import shared_task

from apps.engagement.services.loyalty import expire_due_points, sweep_expired_reservations

__all__ = ["sweep_loyalty"]


@shared_task(name="engagement.sweep_loyalty")
def sweep_loyalty() -> dict:
    """Release stale holds and expire due points. Returns counts for logs/tests."""
    released = sweep_expired_reservations()
    expired = expire_due_points()
    return {"released": released, "expired": expired}
