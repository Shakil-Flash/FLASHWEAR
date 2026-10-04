"""Moderation and service queues: reviews, FLASH Loop, support tickets.

Read-only. Decisions are made by :func:`apps.engagement.services.reviews`,
:func:`apps.loop.services.loop_items` and :func:`apps.support.services.tickets`, which
own the rules (and the audit rows their own domains already write).
"""

from __future__ import annotations

from django.db.models import Prefetch, Q, QuerySet

from apps.backoffice.selectors.common import sort_queryset
from apps.engagement.models import Review
from apps.loop.models import LoopItem, RecycleRequest, ResaleListing, TradeInRequest
from apps.support.models import SupportTicket

__all__ = [
    "REVIEW_SORTS",
    "SUPPORT_SORTS",
    "loop_items",
    "open_support_statuses",
    "pending_loop_count",
    "resale_listings",
    "review_queue",
    "support_tickets",
]

REVIEW_SORTS = {
    "-created_at": ["-created_at", "-pk"],
    "created_at": ["created_at", "pk"],
    "rating": ["rating", "-created_at"],
    "-rating": ["-rating", "-created_at"],
}

SUPPORT_SORTS = {
    "-created_at": ["-created_at", "-pk"],
    "created_at": ["created_at", "pk"],
    "-updated_at": ["-updated_at", "-pk"],
    "priority": ["priority", "-created_at"],
}

#: Loop item states that are waiting for a human.
PENDING_LOOP_STATUSES = (
    LoopItem.Status.SUBMITTED,
    LoopItem.Status.UNDER_REVIEW,
)


def review_queue(
    *, q: str = "", status: str = "", rating: str = "", sort: str = ""
) -> QuerySet:
    """Reviews for moderation, newest first, with author and product joined once."""
    qs = Review.objects.select_related("author", "product", "order")
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(
            Q(product__name__icontains=needle)
            | Q(author__email__icontains=needle)
            | Q(title__icontains=needle)
        )
    if status in Review.Status.values:
        qs = qs.filter(status=status)
    if rating and rating.isdigit() and 1 <= int(rating) <= 5:
        qs = qs.filter(rating=int(rating))
    return sort_queryset(qs, sort, REVIEW_SORTS, "-created_at")[0]


def loop_items(*, status: str = "", type_: str = "", q: str = "", sort: str = "") -> QuerySet:
    """Loop items in the moderation pipeline (or filtered to any state an operator needs).

    ``status=""`` means *the queue* (submitted and under review), not *everything*: a
    default that returned the whole history would bury the work under the archive. The
    literal ``status="all"`` is the escape hatch for a screen that wants every state, and
    an unknown value falls back to the queue rather than matching nothing.
    """
    qs = LoopItem.objects.select_related(
        "user", "product", "variant", "reviewed_by"
    ).prefetch_related(
        Prefetch("images"),
        "resale_listing",
        "trade_in_request",
        "recycle_request",
    )
    if status == "all":
        pass
    elif status in LoopItem.Status.values:
        qs = qs.filter(status=status)
    else:
        # Default: the queue, not the archive.
        qs = qs.filter(status__in=PENDING_LOOP_STATUSES)
    if type_ in LoopItem.Type.values:
        qs = qs.filter(type=type_)
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(Q(user__email__icontains=needle) | Q(title__icontains=needle))
    return qs.order_by("created_at", "pk")


def resale_listings(*, status: str = "") -> QuerySet:
    """Resale listings with the item and seller joined for the moderation tab."""
    qs = ResaleListing.objects.select_related("item", "item__user", "item__product").order_by(
        "-listed_at", "-pk"
    )
    if status in ResaleListing.Status.values:
        qs = qs.filter(status=status)
    return qs


def support_tickets(
    *, q: str = "", status: str = "", priority: str = "", assigned: str = "", sort: str = ""
) -> QuerySet:
    """The support queue as the operations platform sees it: metadata, never transcripts.

    Messages are deliberately *not* joined here. Internal notes are shown by the support
    desk to people who hold the support capability, and a list screen that shipped them
    along with every row would put them in front of everyone who can open the queue.
    """
    qs = SupportTicket.objects.select_related(
        "customer", "assigned_to", "order", "payment", "shipment", "product"
    ).prefetch_related("events")
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(
            Q(number__icontains=needle)
            | Q(subject__icontains=needle)
            | Q(customer__email__icontains=needle)
        )
    if status in SupportTicket.Status.values:
        qs = qs.filter(status=status)
    if priority in SupportTicket.Priority.values:
        qs = qs.filter(priority=priority)
    if assigned == "me":
        qs = qs.filter(assigned_to__isnull=False)
    elif assigned == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)
    return sort_queryset(qs, sort, SUPPORT_SORTS, "-updated_at")[0]


def open_support_statuses() -> tuple[str, ...]:
    """Every support status that is *not* finished -- shared by tiles and alerts."""
    return tuple(
        status
        for status in SupportTicket.Status.values
        if status not in (SupportTicket.Status.RESOLVED, SupportTicket.Status.CLOSED)
    )


def pending_loop_count() -> dict[str, int]:
    """Queue sizes for the Loop tab strip -- four counts, four queries."""
    return {
        "items": LoopItem.objects.filter(status__in=PENDING_LOOP_STATUSES).count(),
        "listings": ResaleListing.objects.filter(
            status=ResaleListing.Status.PENDING_REVIEW
        ).count(),
        "trade_ins": TradeInRequest.objects.filter(
            status__in=(TradeInRequest.Status.SUBMITTED, TradeInRequest.Status.UNDER_REVIEW)
        ).count(),
        "recycling": RecycleRequest.objects.filter(
            status=RecycleRequest.Status.SUBMITTED
        ).count(),
    }
