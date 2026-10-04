"""Badge awarding and the standalone achievement registry (Phase 14).

Two roads to a badge, one destination table:

* **Quest rewards** -- :class:`apps.quests.models.Quest.badge`, issued by
  :mod:`apps.quests.services.rewards` at completion.
* **Achievement rules** -- this module's :data:`ACHIEVEMENTS` registry: predicates over
  authoritative data (a paid order, a published review, a closed loop) checked with
  ``get_or_create`` semantics, so re-checking a thousand times awards once.

Rules are keyed by slug and evaluated server-side; there is no endpoint that awards a
badge and no payload that can claim one. Each rule's copy lives with the rule, so the
Badge row is created lazily with consistent name/description the first time someone
qualifies -- no seed migration to drift out of sync with the code.
"""

from __future__ import annotations

from apps.engagement.models import Review
from apps.loop.models import LoopItem
from apps.orders.models import Order
from apps.quests.models import Badge, UserBadge
from apps.quests.services.handlers import PAID_ORDER_STATUSES

__all__ = ["ACHIEVEMENTS", "award_badge", "award_by_slug", "sync_achievements"]


def award_badge(user, badge: Badge, *, source: str = "") -> tuple[UserBadge, bool]:
    """Award ``badge`` to ``user``. Returns ``(row, created)`` -- created is the once-flag."""
    holder, created = UserBadge.objects.get_or_create(
        user=user,
        badge=badge,
        defaults={"source": source},
    )
    return holder, created


def award_by_slug(
    user,
    slug: str,
    *,
    name: str,
    description: str = "",
    source: str = "",
) -> tuple[UserBadge, bool]:
    """Ensure the Badge row exists, then award it. The lazy-creation half of the registry."""
    badge, _badge_created = Badge.objects.get_or_create(
        slug=slug,
        defaults={"name": name, "description": description},
    )
    return award_badge(user, badge, source=source)


# ---------------------------------------------------------------------------
# Rules: predicates over authoritative data. Pure reads; no side effects.
# ---------------------------------------------------------------------------


def _has_paid_order(user) -> bool:
    return Order.objects.filter(user=user, status__in=PAID_ORDER_STATUSES).exists()


def _has_published_review(user) -> bool:
    return Review.objects.filter(author=user, status=Review.Status.PUBLISHED).exists()


def _closed_a_loop_path(user) -> bool:
    return LoopItem.objects.filter(
        user=user,
        status__in=[
            LoopItem.Status.SOLD,
            LoopItem.Status.TRADE_IN_COMPLETED,
            LoopItem.Status.RECYCLE_COMPLETED,
        ],
    ).exists()


ACHIEVEMENTS: dict[str, dict] = {
    "first-order": {
        "name": "First FLASH",
        "description": "Placed your first paid order.",
        "rule": _has_paid_order,
    },
    "published-reviewer": {
        "name": "Honest Eye",
        "description": "Published your first product review.",
        "rule": _has_published_review,
    },
    "circular-pioneer": {
        "name": "Circular Pioneer",
        "description": "Closed the loop: a resale sold, a trade-in accepted or a recycle done.",
        "rule": _closed_a_loop_path,
    },
}
"""Slug -> {name, description, rule}. Adding one is a code change; the Badge row follows."""


def sync_achievements(user) -> list[UserBadge]:
    """Award every achievement the user currently qualifies for. Returns only new rows."""
    newly_awarded: list[UserBadge] = []
    for slug, spec in ACHIEVEMENTS.items():
        if not spec["rule"](user):
            continue
        holder, created = award_by_slug(
            user,
            slug,
            name=spec["name"],
            description=spec["description"],
            source=f"achievement:{slug}",
        )
        if created:
            newly_awarded.append(holder)
    return newly_awarded
