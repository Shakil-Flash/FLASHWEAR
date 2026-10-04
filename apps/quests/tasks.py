"""Celery tasks for FLASH Quests (Phase 14).

One sweeper, mirroring the inventory / loyalty sweeps: it re-derives progress for users who
already hold a participation row. That covers the gap the lazy read cannot -- a customer
who started a quest, acted somewhere with no signal (added a closet item, saved an outfit),
and never came back. Users with *no* row are deliberately out of scope: there is nothing to
advance, and their first quest read creates rows with correct numbers anyway.

Windows are re-derived inside the same sync, so a sweep that fires after ``end_at`` sees
``ENDED`` and freezes the row -- a late worker can never extend an expired quest.
"""

from __future__ import annotations

from celery import shared_task
from django.contrib.auth import get_user_model

from apps.quests.models import UserQuest
from apps.quests.services.progress import sync_user_quests


@shared_task(name="quests.sweep_progress")
def sweep_progress(limit: int = 1000) -> dict:
    """Re-derive progress for up to ``limit`` distinct users holding active rows."""
    user_ids = list(
        UserQuest.objects.filter(status=UserQuest.Status.ACTIVE)
        .values_list("user_id", flat=True)
        .distinct()[:limit]
    )
    user_model = get_user_model()
    rows_touched = 0
    for user_id in user_ids:
        user = user_model.objects.get(pk=user_id)
        rows_touched += len(sync_user_quests(user))
    return {"users_synced": len(user_ids), "rows_touched": rows_touched}
