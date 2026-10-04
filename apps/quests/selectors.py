"""Read-side selectors for FLASH Quests (Phase 14).

Where a query happens decides what it may contain:

* :func:`published_quests` is the only doorway to player-facing quest lists -- drafts and
  archived rows never leave it, and ordering is fixed server-side (never from a request).
* :func:`user_quest_rows` / :func:`user_badges` are scoped by ``user`` at the top so a
  caller cannot forget it: there is no code path that reads another customer's progress.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.quests.models import Quest, UserBadge, UserQuest

__all__ = ["published_quests", "user_badges", "user_quest_rows"]


def published_quests() -> QuerySet[Quest]:
    """Player-facing quests, in display order. Window state is derived per row at read time."""
    return Quest.objects.filter(publish_state=Quest.PublishState.PUBLISHED)


def user_quest_rows(user) -> QuerySet[UserQuest]:
    """This user's participation rows (their quests only), newest write first."""
    return UserQuest.objects.filter(user=user).select_related("quest")


def user_badges(user) -> QuerySet[UserBadge]:
    """This user's badges with their definitions, newest first."""
    return UserBadge.objects.filter(user=user).select_related("badge")
