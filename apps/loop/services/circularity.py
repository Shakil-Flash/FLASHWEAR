"""Circularity facts for the FLASH Loop detail pages (Phase 13).

Everything here is either a **lookup** (is this item resale/trade-in/
recycling eligible?) or a clearly-labelled **estimate** using a
documented method. There is deliberately no CO2 arithmetic: the
project does not have a documented methodology for it yet, and a
fabricated number ("saves exactly 18.4kg of CO2") is worse than no
number at all.

Lifecycle extension is the one estimate offered, and it is presented
as a range from a fixed table keyed by material category, always
prefixed with "about". Swap in a real methodology when one lands; the
surface the templates read (``circularity_info``) stays stable.
"""

from __future__ import annotations

from django.conf import settings

from apps.loop.models import LoopItem
from apps.loop.services.eligibility import is_recyclable

__all__ = ["circularity_info"]

# Documented, conservative ranges keyed by material/category slug. Every
# value is "about" and must be rendered as such.
_LIFECYCLE_EXTENSION_MONTHS: dict[str, tuple[int, int]] = {
    "default": (6, 18),
    "denim": (12, 24),
    "wool": (12, 30),
    "organic-cotton": (6, 18),
    "cotton": (6, 15),
    "polyester": (12, 24),
    "leather": (24, 60),
}


def _primary_material_slug(loop_item: LoopItem) -> str:
    """Best-effort material slug for the estimate table."""
    if loop_item.product_id:
        materials = list(loop_item.product.materials.all())
        if materials:
            return materials[0].slug.lower()
        if loop_item.product.category_id:
            return loop_item.product.category.slug.lower()
    if loop_item.closet_item is not None and loop_item.closet_item.material:
        return loop_item.closet_item.material.split(",")[0].strip().lower().replace(" ", "-")
    return "default"


def _lifecycle_extension(loop_item: LoopItem) -> tuple[int, int]:
    """Rough "wears-kept-in-use" range for the material. Always an estimate."""
    slug = _primary_material_slug(loop_item)
    return _LIFECYCLE_EXTENSION_MONTHS.get(slug, _LIFECYCLE_EXTENSION_MONTHS["default"])


def _is_flashwear_product(loop_item: LoopItem) -> bool:
    """In-house FLASHWEAR goods only, same rule as the ownership service."""
    if not loop_item.product_id:
        return False
    product = loop_item.product
    if product.brand is None:
        return True
    return product.brand.slug in settings.LOOP_ELIGIBLE_BRAND_SLUGS


def circularity_info(loop_item: LoopItem) -> dict:
    """Facts the public detail page may show, with no private data.

    Returns a dict of primitives (JSON-safe for the API too):
    ``resale_eligible``, ``trade_in_eligible``, ``recycle_eligible``,
    ``material_category``, ``lifecycle_extension_months``,
    ``is_estimate``, ``authenticity_status`` and ``condition``.
    """
    is_flashwear = _is_flashwear_product(loop_item)
    recyclable = False
    if loop_item.product_id:
        recyclable, _ = is_recyclable(loop_item.product)

    low, high = _lifecycle_extension(loop_item)
    return {
        "type": loop_item.type,
        "condition": loop_item.condition,
        "condition_label": loop_item.get_condition_display(),
        "authenticity_status": loop_item.authenticity_status,
        "authenticity_label": loop_item.get_authenticity_status_display(),
        "resale_eligible": is_flashwear,
        "trade_in_eligible": is_flashwear,
        "recycle_eligible": recyclable,
        "material_category": _primary_material_slug(loop_item),
        "lifecycle_extension_months": [low, high],
        "lifecycle_extension_is_estimate": True,
    }
