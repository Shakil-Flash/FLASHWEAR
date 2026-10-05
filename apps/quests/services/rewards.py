"""Issue the rewards a completed quest promises (Phase 14).

Rewards are issued **exactly once, automatically, at the moment of completion** -- there is
no claim endpoint, no pending state and no client involvement. Two independent locks make
"exactly once" true rather than hoped-for:

* the progress engine completes a row with a conditional ``UPDATE ... WHERE status='active'``
  (see :mod:`apps.quests.services.progress`), so two concurrent syncs race to one winner;
* FLASH Points are written through ``PointsTransaction.get_or_create`` keyed on
  ``(reference, type)`` -- the same ledger idempotency Phase 7 uses for order earns -- so
  even a replayed completion cannot mint a second credit.

The reference format is ``quest:<quest id>:user:<user id>`` plus ``:<period key>`` for
repeatable quests. The quest **id**, never its slug: staff may rename a slug without
re-deriving history, and a duplicated reference would silently double-award. Badges are
one ``(user, badge)`` row by unique constraint, so their idempotency is structural.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.engagement.models import PointsTransaction
from apps.notifications.models import NotificationType
from apps.quests.models import UserBadge, UserQuest
from apps.quests.services import badges as badge_service
from apps.quests.services.errors import QuestConfigurationError

__all__ = ["issue_badge", "issue_points", "issue_rewards", "reward_reference"]


def _notify(user, notification_type: str, key: str, *, context, action_url, related_id):
    """Phase 17: a customer-facing notice for a quest event. Never raises; runs in the
    completion transaction so the notification rolls back with the state change.
    """
    from apps.notifications.services.events import emit

    emit(
        notification_type=notification_type,
        user=user,
        idempotency_key=key,
        context=context,
        action_url=action_url,
        related_object_type="quest",
        related_object_id=related_id,
    )


def reward_reference(quest, user, period_key: str) -> str:
    """The deterministic ledger reference for one completion of one quest by one user."""
    ref = f"quest:{quest.pk}:user:{user.pk}"
    if period_key:
        ref = f"{ref}:{period_key}"
    max_length = PointsTransaction._meta.get_field("reference").max_length
    if len(ref) > max_length:
        # Unreachable with integer PKs (comfortably under 64); loud rather than truncated,
        # because a truncated reference would collide with a *different* completion.
        raise QuestConfigurationError(
            f"Reward reference exceeds the ledger's {max_length}-character limit: {ref!r}",
        )
    return ref


def issue_points(user, quest, period_key: str) -> PointsTransaction | None:
    """Credit the quest's FLASH Points through the Phase 7 ledger. Idempotent."""
    if quest.points_reward <= 0:
        return None
    entry, created = PointsTransaction.objects.get_or_create(
        reference=reward_reference(quest, user, period_key),
        transaction_type=PointsTransaction.TransactionType.BONUS,
        defaults={
            "user_id": user.pk,
            "amount": int(quest.points_reward),
            "expires_at": timezone.now() + timedelta(days=int(settings.LOYALTY_EXPIRY_DAYS)),
            "note": f"Quest reward: {quest.name}",
        },
    )
    if created:
        from django.urls import reverse

        _notify(
            user,
            NotificationType.QUEST_REWARD_GRANTED,
            f"quest:{quest.pk}:reward_granted:{user.pk}:{period_key or '-'}",
            context={
                "reward_name": quest.name,
                "reward_description": getattr(quest, "description", "") or "",
            },
            action_url=reverse("account:quests"),
            related_id=quest.pk,
        )
    return entry


def issue_badge(user, quest) -> UserBadge | None:
    """Award the quest's badge, if it has one. Unique (user, badge) makes replays no-ops."""
    if quest.badge is None:
        return None
    holder, _created = badge_service.award_badge(user, quest.badge, source=f"quest:{quest.slug}")
    return holder


def issue_rewards(user_quest: UserQuest) -> dict:
    """All rewards for a just-completed row: points, badge, then standalone achievements.

    Called only from the completion path (and safe to call again -- every step is
    idempotent). Standalone achievements are checked here too, so completing a first
    purchase quest also unlocks the "first order" badge without a second event.
    """
    user = user_quest.user
    quest = user_quest.quest
    from django.urls import reverse

    _notify(
        user,
        NotificationType.QUEST_COMPLETED,
        f"quest:{quest.pk}:completed:{user_quest.pk}",
        context={"quest_name": quest.name},
        action_url=reverse("account:quest-detail", args=[quest.slug]),
        related_id=quest.pk,
    )
    return {
        "points": issue_points(user, quest, user_quest.period_key),
        "badge": issue_badge(user, quest),
        "achievements": badge_service.sync_achievements(user),
    }
