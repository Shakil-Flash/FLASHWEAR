"""Loop-path eligibility (Phase 13).

Eligibility is always **server-calculated** and never templated:

* resale / trade-in -- ownership evidence (see
  :mod:`apps.loop.services.ownership`) plus the item not already
  being inside an incompatible live loop workflow;
* recycling -- a product-level policy
  (:func:`is_recyclable`) based on materials and category, with
  an honest reason when a product is outside the programme.

A closet item already flowing through a *live* loop item cannot
be submitted again: the database partial-unique constraints are
the hard backstop, these functions are the friendly front door.
"""

from __future__ import annotations

from django.conf import settings

from apps.catalog.models import Product
from apps.loop.models import LoopItem
from apps.loop.services.errors import EligibilityError
from apps.loop.services.ownership import OwnershipEvidence, verify_ownership

__all__ = [
    "check_recycling_eligibility",
    "is_recyclable",
    "verify_loop_eligibility",
]


def active_loop_item_for(evidence: OwnershipEvidence) -> LoopItem | None:
    """A live loop item for the same physical unit, if one exists."""
    if evidence.closet_item is not None:
        return (
            LoopItem.objects.filter(
                closet_item=evidence.closet_item,
            )
            .exclude(status__in=LoopItem.terminal_statuses())
            .first()
        )
    if evidence.order_item is not None:
        return (
            LoopItem.objects.filter(
                order_item=evidence.order_item,
                closet_item__isnull=True,
            )
            .exclude(status__in=LoopItem.terminal_statuses())
            .first()
        )
    return None


def verify_loop_eligibility(
    user,
    loop_type: str,
    *,
    closet_item_id: int | None = None,
    order_item_id: int | None = None,
    variant_id: int | None = None,
    product_id: int | None = None,
) -> OwnershipEvidence:
    """Verify ownership **and** loop eligibility for a new submission.

    Raises :class:`~apps.loop.services.errors.OwnershipError` or
    :class:`~apps.loop.services.errors.EligibilityError`.
    """
    if loop_type not in dict(LoopItem.Type.choices):
        raise EligibilityError("Unknown loop path.", code="loop_bad_type")

    evidence = verify_ownership(
        user,
        closet_item_id=closet_item_id,
        order_item_id=order_item_id,
        variant_id=variant_id,
        product_id=product_id,
    )

    existing = active_loop_item_for(evidence)
    if existing is not None:
        raise EligibilityError(
            "This item is already moving through the FLASH Loop "
            f"({existing.get_status_display().lower()}).",
            code="loop_item_already_active",
        )
    return evidence


def is_recyclable(product: Product) -> tuple[bool, str]:
    """Whether the recycling programme currently accepts ``product``.

    A product is recyclable when any of its materials, or its most
    specific category slug, is in the configured programme scope.
    The second return value is a user-facing reason, so a product
    outside the programme gets an honest answer rather than a
    generic error.
    """
    recyclable_materials = {slug.lower() for slug in settings.LOOP_RECYCLABLE_MATERIALS}
    recyclable_categories = {
        slug.lower() for slug in settings.LOOP_RECYCLABLE_CATEGORIES
    }

    material_slugs = {
        material.slug.lower() for material in product.materials.all()
    }
    accepted_materials = material_slugs & recyclable_materials
    if accepted_materials:
        return True, ""

    category_slug = (product.category.slug.lower() if product.category else "")
    if category_slug in recyclable_categories:
        return True, ""

    return (
        False,
        "This item's materials are not currently accepted by the FLASHWEAR "
        "recycling programme.",
    )


def check_recycling_eligibility(product: Product) -> None:
    """Raise :class:`EligibilityError` when ``product`` is not recyclable."""
    recyclable, reason = is_recyclable(product)
    if not recyclable:
        raise EligibilityError(reason, code="loop_not_recyclable")
