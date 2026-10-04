"""Account-area screens for FLASH Quests & Rewards (Phase 14).

Three customer surfaces, all session-authenticated and personal:

* ``/account/quests/`` -- the quest list, bucketed by derived state (active / completed /
  upcoming / ended). Opening it *is* the lazy recompute: each published quest's progress
  is re-derived from authoritative data and persisted, which is how a customer who acted
  somewhere with no signal gets credit on their next visit.
* ``/account/quests/<slug>/`` -- one quest: the rule, the progress bar, the reward.
* ``POST /account/quests/<slug>/start/`` -- explicit participation. The only mutation a
  customer can trigger, and it writes a zero-progress row -- never progress, never a
  reward. Completion has no endpoint at all: the engine owns it.
* ``/account/rewards/`` -- badges, completed quests and the FLASH Points summary, marked
  ``noindex`` (private account data, like the rest of the area).

Deep links ("Start shopping", "Open my closet") resolve from a fixed map -- a quest's CTA
never comes from user input, and a type without a sensible target simply has no button.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.engagement.services import loyalty
from apps.quests import selectors
from apps.quests.models import Quest, UserQuest
from apps.quests.services import progress as progress_service
from apps.quests.services.badges import sync_achievements
from apps.quests.services.errors import QuestError

# quest_type -> (button label, view name). Missing entry means "no CTA button" (the DNA
# quest's action lives behind an API with no customer page yet; the creator app is dormant).
QUEST_CTAS: dict[str, tuple[str, str]] = {
    Quest.QuestType.ORDER_PAID: ("Shop now", "catalog:product-list"),
    Quest.QuestType.REVIEW_WRITTEN: ("Find something to review", "catalog:product-list"),
    Quest.QuestType.CLOSET_ADD: ("Open my closet", "account:closet"),
    Quest.QuestType.OUTFIT_SAVED: ("Build an outfit", "account:outfits"),
    Quest.QuestType.WISHLIST_ADD: ("Open my wishlist", "shop:wishlist"),
    Quest.QuestType.DROP_PURCHASED: ("Shop the latest drop", "catalog:product-list"),
    Quest.QuestType.LOOP_RESALE: ("Open FLASH Loop", "account:loop"),
    Quest.QuestType.LOOP_TRADE_IN: ("Open FLASH Loop", "account:loop"),
    Quest.QuestType.LOOP_RECYCLE: ("Open FLASH Loop", "account:loop"),
    Quest.QuestType.PROFILE_COMPLETED: ("Complete my profile", "account:profile"),
}


def _cta_for(quest_type: str) -> tuple[str, str] | None:
    """Resolve the quest's deep link, tolerating a renamed route (button just disappears)."""
    entry = QUEST_CTAS.get(quest_type)
    if entry is None:
        return None
    label, url_name = entry
    try:
        return label, reverse(url_name)
    except NoReverseMatch:
        return None


def _bucket(view: progress_service.QuestProgress) -> str:
    """Which section of the list this quest belongs to. Completed wins over window state."""
    if view.is_completed:
        return "completed"
    if view.state == Quest.State.ACTIVE:
        return "active"
    if view.state == Quest.State.SCHEDULED:
        return "scheduled"
    return "ended"  # ENDED; drafts/archived never reach the selector


@login_required
@never_cache
def quest_list(request):
    """``/account/quests/`` -- every published quest with this customer's live progress."""
    buckets: dict[str, list[dict]] = {"active": [], "completed": [], "scheduled": [], "ended": []}
    for quest in selectors.published_quests():
        view = progress_service.progress_for(request.user, quest)
        buckets[_bucket(view)].append(
            {"qp": view, "cta": _cta_for(quest.quest_type)},
        )

    context = {
        "active_quests": buckets["active"],
        "completed_quests": buckets["completed"],
        "scheduled_quests": buckets["scheduled"],
        "ended_quests": buckets["ended"],
        "points_balance": loyalty.balance_for(request.user),
        "badge_count": selectors.user_badges(request.user).count(),
    }
    return render(request, "account/quests.html", context)


@login_required
@never_cache
def quest_detail(request, slug: str):
    """``/account/quests/<slug>/`` -- the rule, the bar, the reward, for this customer."""
    quest = get_object_or_404(selectors.published_quests(), slug=slug)
    view = progress_service.progress_for(request.user, quest)
    context = {
        "quest": quest,
        "quest_progress": view,
        "cta": _cta_for(quest.quest_type),
        "can_start": view.state == Quest.State.ACTIVE and not view.is_started,
    }
    return render(request, "account/quest_detail.html", context)


@login_required
@require_POST
def quest_start(request, slug: str):
    """``POST /account/quests/<slug>/start/`` -- join a quest. Never touches progress."""
    quest = get_object_or_404(selectors.published_quests(), slug=slug)
    try:
        row = progress_service.start_quest(request.user, quest)
    except QuestError as exc:
        messages.error(request, exc.message)
    else:
        if row.is_completed:
            messages.success(request, f"\u201c{quest.name}\u201d is already complete.")
        else:
            messages.success(request, f"Quest started: {quest.name}.")
    return redirect("account:quest-detail", slug=slug)


@login_required
@never_cache
def rewards_dashboard(request):
    """``/account/rewards/`` -- badges, completed quests and the points summary.

    Re-checks standalone achievements on the way in (the same lazy pattern as quest
    progress): an order placed without ever opening the quest area still earns its badge
    the first time the customer looks at their rewards.
    """
    user = request.user
    if sync_achievements(user):
        messages.success(request, "New badge unlocked!")

    rows = selectors.user_quest_rows(user)
    completed_rows = rows.filter(status=UserQuest.Status.COMPLETED).select_related("quest")[:20]
    context = {
        "balance": loyalty.balance_for(user),
        "held": loyalty.held_for(user),
        "badges": list(selectors.user_badges(user)),
        "completed_quests": list(completed_rows),
        "completed_count": rows.filter(status=UserQuest.Status.COMPLETED).count(),
        "active_count": rows.filter(status=UserQuest.Status.ACTIVE).count(),
        "expirations": loyalty.upcoming_expirations(user, limit=5),
    }
    return render(request, "account/rewards.html", context)
