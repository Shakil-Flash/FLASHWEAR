"""Deterministic trade-in valuation (Phase 13).

A policy calculator, **not** an AI model and **not** a guarantee. The
estimate is shown next to the words "estimated"; the reviewer's final
credit (``TradeInRequest.final_credit``) is what gets awarded, and only
after inspection.

The formula, all configurable from settings:

    estimate = base_value
             x condition multiplier
             x category multiplier
             x age multiplier
             and never above ``MAX_FRACTION`` of the original unit price

Every factor is a ``Decimal`` and the result is rounded HALF_UP to two
places, so the same inputs always produce the same number: an operator
can reproduce any estimate from the settings block alone. There is no
network call, no randomness and no model inference anywhere in this
module -- a valuation that cannot be audited is a valuation that
cannot be trusted with a customer's money.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.utils import timezone

from apps.loop.models import LoopItem

__all__ = ["estimate_trade_in_credit"]

# Condition -> multiplier. These are the built-in fallbacks; a deployment
# that defines LOOP_TRADE_IN_CONDITION_MULTIPLIERS overrides them entirely.
_DEFAULT_CONDITION_MULTIPLIERS: dict[str, Decimal] = {
    LoopItem.Condition.NEW_WITH_TAGS: Decimal("0.55"),
    LoopItem.Condition.LIKE_NEW: Decimal("0.45"),
    LoopItem.Condition.EXCELLENT: Decimal("0.35"),
    LoopItem.Condition.GOOD: Decimal("0.25"),
    LoopItem.Condition.FAIR: Decimal("0.15"),
    LoopItem.Condition.DAMAGED: Decimal("0.05"),
}

# Category slug (or "default") -> multiplier. Sparse on purpose: any
# category missing from the mapping falls back to ``default``.
_DEFAULT_CATEGORY_MULTIPLIERS: dict[str, Decimal] = {
    "default": Decimal("1.00"),
}


def _condition_multipliers() -> dict[str, Decimal]:
    configured = getattr(settings, "LOOP_TRADE_IN_CONDITION_MULTIPLIERS", None)
    if not configured:
        return _DEFAULT_CONDITION_MULTIPLIERS
    return {key: Decimal(str(value)) for key, value in configured.items()}


def _category_multipliers() -> dict[str, Decimal]:
    configured = getattr(settings, "LOOP_TRADE_IN_CATEGORY_MULTIPLIERS", None)
    if not configured:
        return _DEFAULT_CATEGORY_MULTIPLIERS
    return {key: Decimal(str(value)) for key, value in configured.items()}


def _age_multiplier(age_days: int | None) -> Decimal:
    """Depreciate by purchase/closet age using the configured brackets.

    ``LOOP_TRADE_IN_AGE_BRACKETS`` is an ordered tuple of
    ``(age_in_days_or_None, multiplier)``: the first bracket whose
    ceiling the age fits under wins, ``None`` meaning "and everything
    older". A missing age (no order line and no closet row) earns the
    full multiplier rather than a penalty for missing data.
    """
    if age_days is None:
        return Decimal("1.00")
    for ceiling, multiplier in settings.LOOP_TRADE_IN_AGE_BRACKETS:
        if ceiling is None or age_days <= ceiling:
            return Decimal(str(multiplier))
    return Decimal("1.00")


def _original_unit_price(loop_item: LoopItem) -> Decimal | None:
    """The garment's first-party price when it can be determined."""
    if loop_item.order_item is not None:
        return loop_item.order_item.unit_price
    if loop_item.variant_id is not None:
        return loop_item.variant.price
    if loop_item.closet_item is not None and loop_item.closet_item.variant_id:
        return loop_item.closet_item.variant.price
    return None


def _purchase_age_days(loop_item: LoopItem) -> int | None:
    """Days since the purchase (the order's creation) or closet add."""
    anchor = None
    if loop_item.order_item is not None:
        anchor = loop_item.order_item.order.created_at
    elif loop_item.closet_item is not None:
        anchor = loop_item.closet_item.created_at
    if anchor is None:
        return None
    delta = timezone.now() - anchor
    return max(0, delta.days)


def estimate_trade_in_credit(loop_item: LoopItem) -> Decimal:
    """Return the policy estimate for ``loop_item``'s trade-in credit.

    Pure and synchronous. The caller decides whether the estimate is
    shown (always labelled "estimated") and whether the final credit
    differs after inspection.
    """
    base = Decimal(str(settings.LOOP_TRADE_IN_BASE_VALUE))

    condition = _condition_multipliers().get(loop_item.condition, Decimal("1.00"))

    category_map = _category_multipliers()
    category_key = "default"
    if loop_item.product_id and loop_item.product.category_id:
        category_key = loop_item.product.category.slug.lower()
    category = category_map.get(category_key, category_map.get("default", Decimal("1.00")))

    age = _age_multiplier(_purchase_age_days(loop_item))

    estimate = base * condition * category * age

    original_price = _original_unit_price(loop_item)
    if original_price is not None and original_price > 0:
        ceiling = original_price * settings.LOOP_TRADE_IN_MAX_FRACTION_OF_PRICE
        if estimate > ceiling:
            estimate = ceiling

    if estimate < 0:
        estimate = Decimal("0.00")
    return estimate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
