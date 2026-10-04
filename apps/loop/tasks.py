"""FLASH Loop Celery tasks (Phase 13).

Only housekeeping lives here: expiring stale listings and credits.
**No correctness depends on a task running** -- the database state
(and the selectors' time-aware filters) remain authoritative, so a
worker that is down delays cleanup but never corrupts a listing,
a price or a credit balance.
"""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.loop.models import LoopCredit, ResaleListing

__all__ = ["expire_loop_credits", "expire_stale_listings"]


@shared_task(name="loop.expire_stale_listings")
def expire_stale_listings() -> int:
    """Expire ACTIVE listings older than ``LOOP_LISTING_TTL_DAYS``.

    Returns the number of listings expired (handy in tests and logs).
    The loop item itself is left ``LISTED``: an expired listing is a
    shelf decision, and the customer may relist through the normal
    moderation flow later.
    """
    ttl_days = getattr(settings, "LOOP_LISTING_TTL_DAYS", 90)
    cutoff = timezone.now() - timedelta(days=ttl_days)
    stale = ResaleListing.objects.filter(
        status=ResaleListing.Status.ACTIVE,
        listed_at__isnull=False,
        listed_at__lt=cutoff,
    )
    return stale.update(
        status=ResaleListing.Status.EXPIRED,
        expired_at=timezone.now(),
        updated_at=timezone.now(),
    )


@shared_task(name="loop.expire_loop_credits")
def expire_loop_credits() -> int:
    """Mark past-due credits EXPIRED so admin lists and reporting agree.

    The public balance selector already excludes expired credits by
    ``expires_at``; this keeps the stored status in step too.
    """
    now = timezone.now()
    stale = LoopCredit.objects.filter(
        status=LoopCredit.Status.AWARDED,
        expires_at__isnull=False,
        expires_at__lt=now,
    )
    return stale.update(status=LoopCredit.Status.EXPIRED, updated_at=now)
