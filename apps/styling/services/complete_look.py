"""Complete the Look (Phase 21, spec Section 2).

The PDP's complementary-pieces widget: given the product on screen, return the wardrobe
roles that would finish the outfit (a top needs a bottom and shoes; a dress needs shoes and
something to go with it...) and the single best buyable candidate for each role -- scored
with the same explainable engine the studio uses, minus the customer's own closet, because
this is a shopping surface.

Everything stays deterministic and prefetch-safe: one pool query, no per-card queries, no
external calls. The widget renders as an htmx fragment so the product page keeps its
query-count budget; this module only computes what that fragment shows.
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.closet.services.closet import guess_category
from apps.styling.models.flash_dna import get_flash_dna
from apps.styling.services.catalog_pool import product_price, suggestable_products
from apps.styling.services.outfit_generation import MAX_PIECE_REASONS, _ranked
from apps.styling.services.signals import build_style_signals

__all__ = [
    "COMPLEMENTARY_ROLES",
    "CompleteLook",
    "LookItem",
    "complete_look",
    "complete_look_to_dict",
]

#: Source wardrobe category -> the categories that finish a look with it, best first.
COMPLEMENTARY_ROLES: dict[str, tuple[str, ...]] = {
    "tops": ("bottoms", "shoes", "accessories"),
    "bottoms": ("tops", "shoes"),
    "dresses": ("shoes", "accessories"),
    "outerwear": ("tops", "bottoms"),
    "shoes": ("tops", "bottoms"),
    "accessories": ("tops", "bottoms", "dresses"),
}

#: Fallback when taxonomy guessing cannot classify the source product.
DEFAULT_COMPLEMENTS: tuple[str, ...] = ("tops", "bottoms", "shoes")

#: How many pieces one look may return (a widget row, not a whole outfit).
DEFAULT_LIMIT = 3


@dataclass
class LookItem:
    """One complementary catalogue piece, with the reason it belongs next to the source."""

    product: object
    complements: str  # the source category this piece pairs with
    score: float
    reasons: tuple[str, ...]
    price: object | None = None  # Decimal | None

    @property
    def pk(self) -> int | None:
        return self.product.pk if self.product else None

    @property
    def token(self) -> str:
        return f"p{self.pk}" if self.pk is not None else ""


@dataclass
class CompleteLook:
    """The widget result: source product context, complementary items, graceful warnings."""

    source_product: object
    source_category: str
    items: list[LookItem]
    warnings: tuple[str, ...]

    @property
    def has_items(self) -> bool:
        return bool(self.items)


def complete_look(
    product, *, user=None, occasion: str = "", limit: int = DEFAULT_LIMIT
) -> CompleteLook:
    """Return up to *limit* complementary buyable pieces for *product*.

    Scoring reuses the studio engine's explainable ranker with an everyday occasion (the
    widget has no occasion context of its own): FLASH DNA colours, styles, brands and price
    ceilings all still apply when the visitor is signed in, and anonymous visitors simply
    get the strongest structural pairings.
    """
    source_category = str(guess_category(product))
    signals = build_style_signals(dna=get_flash_dna(user), occasion=occasion)
    wanted = COMPLEMENTARY_ROLES.get(source_category, DEFAULT_COMPLEMENTS)

    buckets = suggestable_products(roles=frozenset(wanted), exclude=frozenset({product.pk}))

    items: list[LookItem] = []
    for category in wanted:
        if len(items) >= limit:
            break
        bucket = buckets.get(category)
        if not bucket:
            continue
        ranked = _ranked([], bucket, signals=signals, season="")
        if not ranked:
            continue
        candidate, score, affinity = ranked[0]
        pair_line = f"Pairs with {product.name}."
        reasons = (pair_line, *affinity[: MAX_PIECE_REASONS - 1])
        items.append(
            LookItem(
                product=candidate,
                complements=source_category,
                score=score,
                reasons=reasons,
                price=product_price(candidate),
            )
        )

    warnings: tuple[str, ...] = ()
    if not items:
        warnings = ("No matching pieces right now -- check back soon.",)

    return CompleteLook(
        source_product=product,
        source_category=source_category,
        items=items,
        warnings=warnings,
    )


def complete_look_to_dict(look: CompleteLook) -> dict:
    """JSON-safe view of the widget for the API variant."""
    source = look.source_product
    return {
        "source": {
            "product_id": source.pk,
            "slug": source.slug,
            "name": source.name,
            "url": source.get_absolute_url(),
            "category": look.source_category,
        },
        "items": [_item_dict(item) for item in look.items],
        "warnings": list(look.warnings),
    }


def _item_dict(item: LookItem) -> dict:
    """JSON-safe view of one complementary piece (variants included for add-to-bag)."""
    product = item.product
    image = product.primary_image
    return {
        "product_id": product.pk,
        "slug": product.slug,
        "name": product.name,
        "brand": product.brand.name if product.brand else "",
        "url": product.get_absolute_url(),
        "image": image.image.url if image else None,
        "price": str(item.price) if item.price is not None else None,
        "token": item.token,
        "reasons": list(item.reasons),
        "variants": [
            {
                "id": variant.pk,
                "label": variant.option_label,
                "price": str(variant.price) if variant.price is not None else None,
            }
            for variant in product.purchasable_variants
        ],
    }
