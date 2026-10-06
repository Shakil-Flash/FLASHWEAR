"""Buyable catalogue pieces bucketed by wardrobe role (Phase 21).

One queryset builder shared by the smart-outfit generator and Complete the Look, so both
features agree on what "suggestable" means:

* published (``Product.objects.published()``),
* at least one active variant that is **not known** to be out of stock -- a variant without a
  stock row reads as available (the catalogue ships that way), a variant whose stock shows
  ``available <= 0`` is excluded, and a product with no buyable variant left drops out,
* cheapest buyable price for budget checks.

Availability is filtered *inside the variants prefetch*, so a bucket of forty products costs
a fixed handful of queries rather than one stock lookup per card.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import F, Prefetch, Q

from apps.catalog.models import Product, ProductImage, ProductVariant
from apps.closet.services.closet import guess_category

__all__ = ["product_price", "suggestable_products"]


#: How many published products one pool may consider; enough for diversity, bounded for speed.
POOL_LIMIT = 60


def _buyable_variant_queryset():
    """Active variants a customer could actually add to a bag right now."""
    return (
        ProductVariant.objects.filter(is_active=True)
        .filter(Q(stock__isnull=True) | Q(stock__on_hand__gt=F("stock__reserved")))
        .select_related("color", "size")
    )


def suggestable_products(
    *,
    roles: set[str] | frozenset[str] | None = None,
    exclude: set[int] | frozenset[int] = frozenset(),
    limit: int = POOL_LIMIT,
) -> dict[str, list]:
    """Published, buyable products grouped by wardrobe role (``tops``, ``shoes``, ...).

    ``roles`` restricts the pool to the slots the caller is filling (one query either way);
    ``exclude`` drops ids the caller already uses (the source product in Complete the Look).
    Ordering inside every bucket is stable: featured first, then newest, then id -- which is
    what makes seed-driven rotation reproduce the same look for the same inputs.
    """
    products = (
        Product.objects.published()
        .select_related(
            "brand",
            "fit",
            "category",
            "category__parent",
            "category__parent__parent",
        )
        .prefetch_related(
            Prefetch(
                "images",
                queryset=ProductImage.objects.select_related("variant__color"),
            ),
            Prefetch("variants", queryset=_buyable_variant_queryset()),
            "materials",
            "tags",
        )
        .order_by("-is_featured", "-published_at", "-id")[:limit]
    )

    buckets: dict[str, list] = {}
    for product in products:
        if product.pk in exclude:
            continue
        # The prefetch already dropped inactive and sold-out variants; a product whose
        # surviving set is empty has nothing to buy, so it leaves the pool entirely
        # (this reads the prefetch cache -- no extra query per candidate).
        if not product.purchasable_variants:
            continue
        role = guess_category(product)
        if roles is not None and role not in roles:
            continue
        buckets.setdefault(role, []).append(product)
    return buckets


def product_price(product) -> Decimal | None:
    """Lowest buyable-variant price, or ``None`` when nothing is priced.

    Reads from the prefetch cache built by :func:`suggestable_products` when present, so a
    pool scan never fires a price query per product.
    """
    prices = [
        variant.price for variant in product.purchasable_variants if variant.price is not None
    ]
    return min(prices) if prices else None
