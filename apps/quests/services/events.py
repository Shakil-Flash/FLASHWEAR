"""Events: lightweight invalidation triggers for quest progress (Phase 14).

An event never *contains* progress. It only says "something happened to this user";
the handler re-derives progress from the database with the same code a page load runs
(:func:`apps.quests.services.progress.sync_user_quests`). That is what makes events safe
to drop, replay or run late: the number they produce is a pure function of rows the
platform already wrote, never of the event payload.

Most subsystems have no signal at all (closet, outfits, wishlist, reviews, DNA, loop), and
Phase 14 does not add any -- their progress is discovered lazily on the first quest read
after the action. :data:`EVENT_QUEST_TYPES` therefore stays small: only events that
actually exist in the codebase appear here, plus standalone achievements which piggyback
on the same trigger.
"""

from __future__ import annotations

from apps.quests.models import Quest, UserQuest
from apps.quests.services import badges as badge_service
from apps.quests.services.progress import sync_user_quests

__all__ = ["EVENT_QUEST_TYPES", "record_event"]

EVENT_QUEST_TYPES: dict[str, tuple[str, ...]] = {
    # The one custom commerce signal in the codebase (apps.orders.signals).
    "order_paid": (
        Quest.QuestType.ORDER_PAID,
        Quest.QuestType.DROP_PURCHASED,
    ),
}
"""Event name -> the quest families whose data it could have changed."""


def record_event(user, event: str) -> list[UserQuest]:
    """Re-derive the quests (and achievements) this event could have moved.

    Unknown events are a deliberate no-op: a future subsystem can announce itself here
    without every caller having to know which events exist yet. Returns the participation
    rows touched, so tests and callers can see the effect without re-querying.
    """
    quest_types = EVENT_QUEST_TYPES.get(event)
    if quest_types is None:
        return []

    touched = sync_user_quests(user, quest_types=quest_types)
    badge_service.sync_achievements(user)
    return touched
