"""Calendar buckets for repeatable quests (Phase 14).

One question, asked in one place: *which slice of history does this quest count, and what
string keys the participation row for that slice?*

* Once-only quests count all-time from ``start_at`` (or forever) and key on ``""``.
* Repeatable quests count from the current UTC bucket -- ``20261004`` (daily),
  ``2026-W40`` (ISO week), ``202610`` (monthly) -- and key on the same string, so
  ``(user, quest, period_key)`` can never collide across periods and a new period starts a
  fresh row instead of overwriting history.

``effective_since`` is the intersection of the quest window and the period: action taken
before the quest opened does not count, and action taken before the current bucket does not
leak in from last week.
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.utils import timezone

from apps.quests.models import Quest

__all__ = ["effective_since", "period_key_for", "period_start_for"]


def _utc(now: datetime) -> datetime:
    """The bucket clock: wall time in UTC for aware datetimes, as-is for naive ones."""
    if timezone.is_aware(now):
        return now.astimezone(UTC)
    return now


def period_key_for(quest: Quest, now: datetime) -> str:
    """The participation key for ``quest`` at ``now``: ``""`` for once-only quests."""
    if quest.is_once:
        return ""
    local = _utc(now)
    if quest.repeat_period == Quest.RepeatPeriod.DAILY:
        return local.strftime("%Y%m%d")
    if quest.repeat_period == Quest.RepeatPeriod.WEEKLY:
        iso = local.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if quest.repeat_period == Quest.RepeatPeriod.MONTHLY:
        return local.strftime("%Y%m")
    # Unreachable through the ORM (choices) but loud if a row ever smuggles a bad value.
    raise ValueError(f"Unknown repeat period: {quest.repeat_period!r}")


def period_start_for(quest: Quest, now: datetime) -> datetime | None:
    """Start of the current period, or ``None`` when the period is meaningless (once-only)."""
    if quest.is_once:
        return None
    local = _utc(now)
    if quest.repeat_period == Quest.RepeatPeriod.DAILY:
        return datetime(local.year, local.month, local.day, tzinfo=UTC)
    if quest.repeat_period == Quest.RepeatPeriod.WEEKLY:
        iso = local.isocalendar()
        return datetime.fromisocalendar(iso.year, iso.week, 1).replace(tzinfo=UTC)
    if quest.repeat_period == Quest.RepeatPeriod.MONTHLY:
        return datetime(local.year, local.month, 1, tzinfo=UTC)
    raise ValueError(f"Unknown repeat period: {quest.repeat_period!r}")


def effective_since(quest: Quest, now: datetime) -> datetime | None:
    """The window a handler must count from: max(quest.start_at, period_start).

    ``None`` means "all of history". A quest that opened last Monday never credits last
    month's closet items; a daily quest never credits yesterday's.
    """
    candidates = [d for d in (quest.start_at, period_start_for(quest, now)) if d is not None]
    return max(candidates) if candidates else None
