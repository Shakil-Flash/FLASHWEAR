"""The discount engine: one place that answers "what does this checkout cost?" (Phase 7).

Every surface that shows or freezes a discounted total -- the checkout panel, ``validate_checkout``
and the handoff's re-validation -- calls :func:`compute_discounts` with the same inputs, so the
number a customer sees, the number frozen in the snapshot and the number re-checked at
"Place order" can only disagree if the *world changed* (a window closed, a balance moved), which
is exactly when a disagreement should surface as an error instead of a wrong charge.

Stacking rule (Phase 7 spec): **promotion first, FLASH Points on what remains, shipping added
afterwards, total never negative.** A promotion may opt out of loyalty redemption
(``Promotion.allow_loyalty_redemption``); nothing can opt out of a promotion.

``excluding_checkout`` is threaded down to every balance read: the checkout validating its own
hold must see that hold as free money, or re-validating an already-reserved checkout would fail
against itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

from apps.catalog.templatetags.catalog_extras import money
from apps.engagement.services import loyalty, promotions
from apps.engagement.services.errors import LoyaltyError

TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class DiscountBreakdown:
    """The full discount picture for (subtotal, code, points) at one moment in time."""

    subtotal: Decimal
    promotion_code: str
    promotion: object | None
    promotion_discount: Decimal
    points: int
    loyalty_discount: Decimal

    @property
    def total_discount(self) -> Decimal:
        return (self.promotion_discount + self.loyalty_discount).quantize(TWO_PLACES)

    @property
    def total_after_discount(self) -> Decimal:
        """Eligible subtotal after both discounts -- shipping is not included here."""
        return (self.subtotal - self.total_discount).quantize(TWO_PLACES)


def compute_discounts(
    *,
    user,
    subtotal: Decimal,
    promotion_code: str = "",
    loyalty_points: int = 0,
    excluding_checkout=None,
) -> DiscountBreakdown:
    """Resolve a checkout's discounts, or raise ``PromotionError`` / ``LoyaltyError``.

    Read-only: no reservation is created here (validation does that separately), no counter
    is incremented (only :func:`apps.engagement.services.promotions.consume_usage` may).
    """
    subtotal = Decimal(subtotal).quantize(TWO_PLACES)

    promotion = None
    promotion_discount = Decimal("0.00")
    code = promotions.normalise_code(promotion_code)
    if code:
        promotion = promotions.find_for_checkout(code, user=user, subtotal=subtotal)
        promotion_discount = promotions.calculate_discount(promotion, subtotal)

    eligible = (subtotal - promotion_discount).quantize(TWO_PLACES)

    points = int(loyalty_points or 0)
    loyalty_discount = Decimal("0.00")
    if points:
        if promotion is not None and not promotion.allow_loyalty_redemption:
            raise LoyaltyError(
                "That promotion does not allow FLASH Points redemption.",
                code="not_combinable",
            )
        minimum = Decimal(settings.LOYALTY_MIN_ORDER_AMOUNT)
        if eligible < minimum:
            raise LoyaltyError(
                f"FLASH Points need a subtotal of at least {money(minimum)} after discounts.",
                code="minimum_not_met",
            )
        points = loyalty.validate_redemption(
            user,
            points,
            eligible_subtotal=eligible,
            excluding_checkout=excluding_checkout,
        )
        loyalty_discount = min(loyalty.redeem_discount(points), eligible)

    return DiscountBreakdown(
        subtotal=subtotal,
        promotion_code=code if promotion else "",
        promotion=promotion,
        promotion_discount=promotion_discount,
        points=points,
        loyalty_discount=loyalty_discount,
    )
