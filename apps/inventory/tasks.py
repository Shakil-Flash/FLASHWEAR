"""Periodic inventory work.

Autodiscovered by ``config.celery``. The schedule lives in
``CELERY_BEAT_SCHEDULE`` (``config.settings.base``); with no broker running the
task can still be invoked directly, and tests call the service behind it.
"""

from __future__ import annotations

from celery import shared_task

from apps.inventory.services import sweep_expired_holds

__all__ = ["sweep_expired_reservations"]


@shared_task(name="inventory.sweep_expired_reservations", ignore_result=True)
def sweep_expired_reservations() -> int:
    """Release checkout holds whose deadline passed with no order placed.

    Returns the number of holds released. Idempotent by construction: only
    ``ACTIVE`` rows transition, so overlapping runs (or a run during a redeploy)
    cannot double-count.
    """
    return sweep_expired_holds()
