"""Closet product matching services (Phase 31).

Connects shopping discovery to the customer's private FLASH Closet without
inventing fake compatibility. Reads real wardrobe items in complementary roles.
"""

from __future__ import annotations

from apps.catalog.models import Product
from apps.closet.models import ClosetItem
from apps.closet.services.closet import guess_category
from apps.styling.services.complete_look import COMPLEMENTARY_ROLES, DEFAULT_COMPLEMENTS


def get_closet_matches_for_product(user, product: Product, limit: int = 4) -> list[ClosetItem]:
    """Find active pieces in the customer's wardrobe that naturally pair with *product*.

    Only runs for authenticated customers with active wardrobe items.
    Categorizes the product via the closet taxonomy and retrieves real pieces
    in complementary categories.
    """
    if not user or not user.is_authenticated:
        return []

    if not product or not product.pk:
        return []

    # 1. Determine wardrobe role of the viewed product
    cat = guess_category(product)
    complementary_roles = COMPLEMENTARY_ROLES.get(cat, DEFAULT_COMPLEMENTS)

    # 2. Query customer's active items in complementary roles
    closet_items = list(
        ClosetItem.objects.filter(
            user=user,
            status=ClosetItem.Status.ACTIVE,
            category__in=complementary_roles,
        )
        .select_related("variant__product", "variant__color", "variant__size")
        .prefetch_related("variant__product__images")
        .order_by("-created_at")[: limit * 2]
    )

    if not closet_items:
        return []

    # 3. Prefer items with images or purchased pieces first
    def _rank(item: ClosetItem) -> tuple[int, int]:
        has_img = (
            1 if (item.image or (item.variant and item.variant.product.images.exists())) else 0
        )
        is_purchased = 1 if item.source == ClosetItem.Source.PURCHASED else 0
        return (has_img, is_purchased)

    closet_items.sort(key=_rank, reverse=True)
    return closet_items[:limit]
