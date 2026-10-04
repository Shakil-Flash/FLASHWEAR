"""Read-side selectors for FLASH Loop (Phase 13).

Where a query happens decides what it may contain:

* :func:`public_resale_listings` is the only doorway to the public
  resale shelf. Drafts, rejected, reserved and sold listings never
  leave this function, and private seller data (email, address) is
  never selected -- the serializer simply has nothing to leak.
* :func:`user_loop_items` / :func:`user_dashboard` are scoped by
  ``user`` at the top so callers cannot forget it.
* Every public list is eager-loaded with one fixed plan so a grid of
  twenty cards is a handful of queries, not sixty (query-count tests
  pin this).
"""

from __future__ import annotations

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from apps.loop.models import LoopCredit, LoopItem, RecycleRequest, ResaleListing, TradeInRequest

__all__ = [
    "public_listing",
    "public_resale_listings",
    "resale_counts",
    "user_credits",
    "user_dashboard",
    "user_loop_items",
    "user_recycle_requests",
    "user_resale_listings",
    "user_trade_ins",
]

# Sorting allowlist for the public shelf. Never pass a request value to
# order_by directly -- exactly the catalogue's rule.
PUBLIC_SORT_CHOICES = {
    "newest": ("-listed_at", "-id"),
    "price_asc": ("asking_price", "-listed_at"),
    "price_desc": ("-asking_price", "-listed_at"),
}
DEFAULT_PUBLIC_SORT = "newest"


def public_resale_listings(
    *,
    sort: str = DEFAULT_PUBLIC_SORT,
    category: str = "",
    size: str = "",
    color: str = "",
    condition: str = "",
    material: str = "",
    min_price=None,
    max_price=None,
    q: str = "",
) -> QuerySet[ResaleListing]:
    """Active, publicly visible resale listings.

    Visibility is one definition, enforced here:

    * listing ``ACTIVE`` (published by moderation);
    * loop item still ``LISTED`` (not reserved or sold elsewhere);
    * authenticity not ``REJECTED`` (a listing suspected of being
      counterfeit must disappear from the shelf);
    * product not removed from the catalogue entirely (null product
      would be a data accident, not a listing).

    Everything else -- drafts, pending review, cancelled, expired --
    falls out. Filters reuse the catalogue's vocabularies (category
    slug, size code, colour slug, material slug); unknown values
    narrow to nothing rather than 400-ing, so a stale bookmark shows
    an empty shelf.
    """
    queryset = (
        ResaleListing.objects.filter(
            status=ResaleListing.Status.ACTIVE,
            loop_item__status=LoopItem.Status.LISTED,
            listed_at__isnull=False,
        )
        .exclude(loop_item__authenticity_status=LoopItem.AuthenticityStatus.REJECTED)
        .select_related(
            "loop_item",
            "loop_item__product",
            "loop_item__product__brand",
            "loop_item__product__category",
            "loop_item__variant",
            "loop_item__variant__color",
            "loop_item__variant__size",
        )
        .prefetch_related(
            "loop_item__images",
            "loop_item__product__materials",
        )
    )

    if category:
        queryset = queryset.filter(loop_item__product__category__slug=category)
    if size:
        queryset = queryset.filter(loop_item__variant__size__code=size)
    if color:
        queryset = queryset.filter(loop_item__variant__color__slug=color)
    if condition:
        queryset = queryset.filter(loop_item__condition=condition)
    if material:
        queryset = queryset.filter(loop_item__product__materials__slug=material)
    if min_price is not None:
        queryset = queryset.filter(asking_price__gte=min_price)
    if max_price is not None:
        queryset = queryset.filter(asking_price__lte=max_price)

    text = (q or "").strip()
    if text:
        queryset = queryset.filter(
            Q(loop_item__product__name__icontains=text)
            | Q(loop_item__title__icontains=text)
            | Q(loop_item__description__icontains=text)
        )

    return queryset.order_by(*PUBLIC_SORT_CHOICES.get(sort, PUBLIC_SORT_CHOICES[DEFAULT_PUBLIC_SORT]))


def public_listing(slug: str) -> ResaleListing | None:
    """One listing by slug under the same visibility rules as the shelf."""
    return public_resale_listings().filter(slug=slug).first()


def user_loop_items(user) -> QuerySet[LoopItem]:
    """Every loop item one customer owns, newest first, eager-loaded.

    Scoped by ``user`` here so a view or API cannot forget it; the
    dashboard and the API share the same privacy boundary.
    """
    return (
        LoopItem.objects.filter(user=user)
        .select_related(
            "product",
            "product__brand",
            "product__category",
            "variant",
            "variant__color",
            "variant__size",
            "order_item",
            "closet_item",
        )
        .prefetch_related("images")
        .order_by("-created_at")
    )


def user_resale_listings(user) -> QuerySet[ResaleListing]:
    """One customer's own listings (any status) for the dashboard."""
    return (
        ResaleListing.objects.filter(seller=user)
        .select_related("loop_item")
        .order_by("-created_at")
    )


def user_trade_ins(user) -> QuerySet[TradeInRequest]:
    return TradeInRequest.objects.select_related("loop_item").filter(user=user)


def user_recycle_requests(user) -> QuerySet[RecycleRequest]:
    return RecycleRequest.objects.select_related("loop_item").filter(user=user)


def user_credits(user) -> QuerySet[LoopCredit]:
    """Valid (awarded, unexpired) credits, newest first."""
    now = timezone.now()
    return (
        LoopCredit.objects.filter(user=user)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .filter(status=LoopCredit.Status.AWARDED)
        .order_by("-awarded_at")
    )


def user_dashboard(user) -> dict:
    """Everything the ``/account/loop/`` page renders, in one pass.

    Grouped counts avoid per-section COUNT(*) queries: each section
    reads its slice of two pre-fetched lists instead of issuing its
    own aggregate.
    """
    items = list(user_loop_items(user))
    listings_by_item = {
        listing.loop_item_id: listing
        for listing in user_resale_listings(user)
    }
    trade_ins = {req.loop_item_id: req for req in user_trade_ins(user)}
    recycles = {req.loop_item_id: req for req in user_recycle_requests(user)}

    sections: dict[str, list] = {
        "drafts": [],
        "pending_review": [],
        "active": [],
        "sold": [],
        "trade_ins": [],
        "recycling": [],
        "cancelled": [],
    }
    for item in items:
        listing = listings_by_item.get(item.pk)
        if item.type == LoopItem.Type.TRADE_IN:
            sections["trade_ins"].append((item, trade_ins.get(item.pk)))
        elif item.type == LoopItem.Type.RECYCLE:
            sections["recycling"].append((item, recycles.get(item.pk)))
        elif item.status in (LoopItem.Status.DRAFT,):
            sections["drafts"].append((item, listing))
        elif item.status in (
            LoopItem.Status.SUBMITTED,
            LoopItem.Status.UNDER_REVIEW,
            LoopItem.Status.APPROVED,
        ):
            sections["pending_review"].append((item, listing))
        elif item.status in (LoopItem.Status.LISTED, LoopItem.Status.RESERVED):
            sections["active"].append((item, listing))
        elif item.status == LoopItem.Status.SOLD:
            sections["sold"].append((item, listing))
        else:
            sections["cancelled"].append((item, listing))

    credits = list(user_credits(user))
    return {
        "sections": sections,
        "counts": {key: len(value) for key, value in sections.items()},
        "credits": credits,
        "credit_total": sum((credit.amount for credit in credits), start=0),
        "total_items": len(items),
    }


def resale_counts() -> int:
    """How many listings the shelf would show (for the header count)."""
    return public_resale_listings().aggregate(n=Count("id"))["n"]
