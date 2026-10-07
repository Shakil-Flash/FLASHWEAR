"""Authoritative quest handlers (Phase 14).

Every handler answers one question -- *how many qualifying units has this user actually
done, according to the database?* -- as a plain integer. Three rules hold everywhere:

* **Server-side only.** Handlers receive no payload, no IDs from a client and no state a
  client submitted; they read rows the platform itself wrote. A quest can therefore never
  be completed by a forged request, only by data that already exists.
* **``since`` is a half-open window.** ``None`` means "all of history" (once-only quests);
  a datetime means "from this moment" (quest ``start_at`` and/or the current period
  bucket -- see :mod:`apps.quests.services.periods`). Actions taken before the window are
  invisible to the count, which is what stops last week's closet purge from completing
  this week's quest.
* **Raw, unclamped.** Handlers return the true count; the progress engine clamps it to the
  quest target. A user who bought ten items on a "buy 1" quest has progress 1, not 10.

The registry at the bottom is keyed by :class:`apps.quests.models.Quest.QuestType` -- the
closed allowlist. An unknown key raises :class:`QuestConfigurationError` loudly rather
than counting zero forever. ``apps.quests`` is a leaf, so every installed app it measures
can be imported at module level; only the dormant ``apps.creator`` needs a runtime gate.
"""

from __future__ import annotations

from datetime import datetime

from django.apps import apps as django_apps
from django.db.models import QuerySet

from apps.closet.models import ClosetItem, Outfit
from apps.engagement.models import Review
from apps.loop.models import LoopItem
from apps.orders.models import Order
from apps.quests.models import Quest
from apps.quests.services.errors import QuestConfigurationError
from apps.shop.models import WishlistItem

__all__ = ["HANDLERS", "compute_raw", "handler_for"]

# An order counts once it has actually been paid. CANCELLED never was; REFUNDED was
# (refunds cannot happen from PENDING_PAYMENT), and progress -- like the rewards it
# unlocks -- is a record of what the customer did, not of what they kept.
PAID_ORDER_STATUSES = (
    Order.Status.PAID,
    Order.Status.PROCESSING,
    Order.Status.SHIPPED,
    Order.Status.DELIVERED,
    Order.Status.REFUNDED,
)


def _paid_orders(user, since: datetime | None) -> QuerySet:
    qs = Order.objects.filter(user=user, status__in=PAID_ORDER_STATUSES)
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    return qs


def order_paid(user, quest: Quest, since: datetime | None) -> int:
    """Paid orders (ever-paid states) placed within the window."""
    return _paid_orders(user, since).count()


def drop_purchased(user, quest: Quest, since: datetime | None) -> int:
    """Paid orders containing at least one product that belongs to any FLASH Drop.

    ``distinct()`` matters: an order with two drop products joins twice and must still
    count as one purchase.
    """
    qs = _paid_orders(user, since).filter(items__variant__product__drop_products__isnull=False)
    return qs.distinct().count()


def review_written(user, quest: Quest, since: datetime | None) -> int:
    """Published reviews authored by the user.

    Pending, rejected and hidden rows never count: moderation is the authority on what a
    review *is*, exactly as the storefront treats it.
    """
    qs = Review.objects.filter(author=user, status=Review.Status.PUBLISHED)
    if since is not None:
        qs = qs.filter(published_at__gte=since)
    return qs.count()


def dna_completed(user, quest: Quest, since: datetime | None) -> int:
    """1 when the user's FLASH DNA has at least one preference, else 0.

    Mirrors ``FlashDNA.is_complete`` -- the same bar the stylist uses to decide it has
    something to weight.
    """
    dna = getattr(user, "flash_dna", None)
    if dna is None:
        return 0
    return 1 if dna.is_complete else 0


def closet_add(user, quest: Quest, since: datetime | None) -> int:
    """Wardrobe rows created within the window, any source, active or archived.

    Archiving later does not un-add a piece; the quest counted the action, not the state.
    """
    qs = ClosetItem.objects.filter(user=user)
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    return qs.count()


def outfit_saved(user, quest: Quest, since: datetime | None) -> int:
    """Outfits that reached SAVED within the window (drafts and archives excluded).

    A saved outfit is the durable artefact; archiving it afterwards does not unsay the
    act of saving.
    """
    qs = Outfit.objects.filter(user=user, status=Outfit.Status.SAVED)
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    return qs.count()


def wishlist_add(user, quest: Quest, since: datetime | None) -> int:
    """Distinct products on the user's wishlist saved within the window.

    Distinct, because a wishlist row per (product, variant) would otherwise let one shirt
    in two sizes count twice, and the quest copy says "save 3 items", not "3 rows".
    """
    qs = WishlistItem.objects.filter(wishlist__user=user)
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    return qs.values("product").distinct().count()


def profile_completed(user, quest: Quest, since: datetime | None) -> int:
    """1 when the user has a full name and at least one contact detail (phone or birth date).

    Deliberately deterministic and explainable in one sentence -- profile "completion
    percentage" heuristics drift, and a quest rule has to be auditable.
    """
    profile = getattr(user, "profile", None)
    if profile is None:
        return 0
    has_name = bool(user.first_name and user.last_name)
    has_detail = bool(profile.phone or profile.date_of_birth)
    return 1 if (has_name and has_detail) else 0


def _loop_count(user, quest: Quest, since: datetime | None, *, loop_type: str, statuses) -> int:
    qs = LoopItem.objects.filter(user=user, type=loop_type, status__in=statuses)
    if since is not None:
        # LoopItem has no per-transition timestamp; updated_at is the honest proxy for
        # "reached this state within the window" (the row's last write *is* its last
        # transition in every service path).
        qs = qs.filter(updated_at__gte=since)
    return qs.count()


def loop_resale(user, quest: Quest, since: datetime | None) -> int:
    """Resale items that reached the shelf (LISTED) or beyond (RESERVED / SOLD)."""
    return _loop_count(
        user,
        quest,
        since,
        loop_type=LoopItem.Type.RESALE,
        statuses=[
            LoopItem.Status.LISTED,
            LoopItem.Status.RESERVED,
            LoopItem.Status.SOLD,
        ],
    )


def loop_trade_in(user, quest: Quest, since: datetime | None) -> int:
    """Trade-ins accepted by moderation (ACCEPTED or COMPLETED)."""
    return _loop_count(
        user,
        quest,
        since,
        loop_type=LoopItem.Type.TRADE_IN,
        statuses=[
            LoopItem.Status.TRADE_IN_ACCEPTED,
            LoopItem.Status.TRADE_IN_COMPLETED,
        ],
    )


def loop_recycle(user, quest: Quest, since: datetime | None) -> int:
    """Recycling accepted by moderation (ACCEPTED or COMPLETED)."""
    return _loop_count(
        user,
        quest,
        since,
        loop_type=LoopItem.Type.RECYCLE,
        statuses=[
            LoopItem.Status.RECYCLE_ACCEPTED,
            LoopItem.Status.RECYCLE_COMPLETED,
        ],
    )


def creator_post(user, quest: Quest, since: datetime | None) -> int:
    """Published posts by the user's creator profile -- 0 while ``apps.creator`` is dormant.

    The creator app is shipped but not yet in ``INSTALLED_APPS``; importing its models
    unconditionally would raise at registry time. The gate is therefore "is the app
    installed", not "did the import fail", so a real coding error inside the app never
    masquerades as "zero progress".
    """
    if not django_apps.is_installed("apps.creator") or "apps.creator" not in settings.INSTALLED_APPS:
        return 0

    from apps.creator.models import CreatorPost, CreatorPostStatus, CreatorProfile

    profile = CreatorProfile.objects.filter(user=user).first()
    if profile is None:
        return 0
    qs = CreatorPost.objects.filter(creator=profile, status=CreatorPostStatus.PUBLISHED)
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    return qs.count()


HANDLERS = {
    Quest.QuestType.ORDER_PAID: order_paid,
    Quest.QuestType.REVIEW_WRITTEN: review_written,
    Quest.QuestType.DNA_COMPLETED: dna_completed,
    Quest.QuestType.CLOSET_ADD: closet_add,
    Quest.QuestType.OUTFIT_SAVED: outfit_saved,
    Quest.QuestType.WISHLIST_ADD: wishlist_add,
    Quest.QuestType.DROP_PURCHASED: drop_purchased,
    Quest.QuestType.CREATOR_POST: creator_post,
    Quest.QuestType.LOOP_RESALE: loop_resale,
    Quest.QuestType.LOOP_TRADE_IN: loop_trade_in,
    Quest.QuestType.LOOP_RECYCLE: loop_recycle,
    Quest.QuestType.PROFILE_COMPLETED: profile_completed,
}
"""Quest type -> authoritative counter. Keys must match ``Quest.QuestType`` exactly."""


def handler_for(quest: Quest):
    """The handler registered for ``quest``'s type, or a loud configuration error."""
    try:
        return HANDLERS[quest.quest_type]
    except KeyError as exc:
        raise QuestConfigurationError(
            f"No handler registered for quest type {quest.quest_type!r}.",
        ) from exc


def compute_raw(user, quest: Quest, since: datetime | None) -> int:
    """Dispatch to the quest's handler and coerce the answer to a non-negative int."""
    return max(0, int(handler_for(quest)(user, quest, since)))
