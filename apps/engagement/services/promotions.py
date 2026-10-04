"""Promotion eligibility and consumption -- the one authoritative engine (Phase 7).

Every surface that asks "does this code apply?" goes through :func:`find_for_checkout`; every
surface that *charges* it goes through :func:`consume_usage`. Nothing else reads
``Promotion.used_count`` to make a decision.

Anti-enumeration rule: a code that does not exist raises the **same generic message** as a
malformed one ("That code is not valid."). Once a code exists, its specific problem (expired,
limit reached, minimum not met) is fair to show -- that is the operator's own campaign failing,
not a secret. A guessed code buys the guesser nothing they could not get by being told the truth
about a code they already knew.

Consumption is where the race lives: two checkouts both holding the last use of a promotion.
The engine locks the promotion row (``SELECT ... FOR UPDATE``), re-checks the limits against the
locked row, increments, and lets the database's ``used_count <= usage_limit`` constraint be the
backstop if any of that is ever wrong.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.catalog.templatetags.catalog_extras import money
from apps.engagement.models import Promotion, PromotionUsage
from apps.engagement.services.errors import PromotionError

GENERIC_INVALID_MESSAGE = "That code is not valid."

# The shape a normalised code must have. Anything else is rejected before it reaches the database,
# so a hostile payload never becomes a query parameter.
CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{3,32}$")

TWO_PLACES = Decimal("0.01")


def normalise_code(raw: str | None) -> str:
    """Trim and upper-case what the customer typed. The DB stores only this form."""
    return (raw or "").strip().upper()


def find_for_checkout(
    code: str | None,
    *,
    user,
    subtotal: Decimal,
) -> Promotion:
    """Return the promotion ``code`` represents, or raise :class:`PromotionError`.

    ``subtotal`` is the *eligible* amount the discount would apply to (before any promotion).
    Raises generic errors for unknown/malformed codes; specific ones for known-but-ineligible
    promotions.
    """
    normalised = normalise_code(code)
    if not CODE_PATTERN.match(normalised):
        raise PromotionError(GENERIC_INVALID_MESSAGE, code="not_found")

    promotion = Promotion.objects.filter(code=normalised).first()
    if promotion is None:
        raise PromotionError(GENERIC_INVALID_MESSAGE, code="not_found")

    now = timezone.now()
    if not promotion.is_active:
        raise PromotionError("That code is not active.", code="inactive")
    if now < promotion.starts_at:
        raise PromotionError("That code is not active yet.", code="not_started")
    if now >= promotion.ends_at:
        raise PromotionError("That code has expired.", code="expired")
    if subtotal < promotion.min_order_amount:
        raise PromotionError(
            f"That code needs a subtotal of at least {money(promotion.min_order_amount)}.",
            code="minimum_not_met",
        )
    if promotion.usage_limit is not None and promotion.used_count >= promotion.usage_limit:
        raise PromotionError("That code has reached its usage limit.", code="limit_reached")
    if (
        promotion.per_user_limit is not None
        and uses_by(user, promotion) >= promotion.per_user_limit
    ):
        raise PromotionError("You have already used that code.", code="user_limit_reached")
    return promotion


def uses_by(user, promotion: Promotion) -> int:
    """How many orders this customer already charged the promotion on."""
    return PromotionUsage.objects.filter(promotion=promotion, user=user).count()


def calculate_discount(promotion: Promotion, subtotal: Decimal) -> Decimal:
    """What the promotion takes off ``subtotal``, rounded to the penny, capped at it.

    Percentage discounts round half-up (10% of 49.95 is 5.00, not 4.99) and a fixed discount
    larger than the subtotal simply becomes the subtotal -- the engine never produces a
    negative or over-refunding number.
    """
    if promotion.discount_type == Promotion.DiscountType.PERCENTAGE:
        discount = (subtotal * promotion.discount_value / Decimal("100")).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )
    else:
        discount = promotion.discount_value
    return min(discount, subtotal)


def consume_usage(
    promotion: Promotion,
    *,
    user,
    order,
    discount: Decimal,
) -> PromotionUsage:
    """Grant the discount on ``order`` exactly once, under a row lock.

    Replaying a handoff (same order, same promotion) returns the existing usage without
    touching ``used_count`` -- financial actions must survive retries.
    """
    with transaction.atomic():
        locked = Promotion.objects.select_for_update().get(pk=promotion.pk)

        existing = PromotionUsage.objects.filter(promotion=locked, order=order).first()
        if existing is not None:
            return existing

        now = timezone.now()
        if not locked.is_active or now >= locked.ends_at or now < locked.starts_at:
            raise PromotionError("That code is no longer valid.", code="expired")
        if locked.usage_limit is not None and locked.used_count >= locked.usage_limit:
            raise PromotionError("That code has reached its usage limit.", code="limit_reached")
        if (
            locked.per_user_limit is not None
            and uses_by(user, locked) >= locked.per_user_limit
        ):
            raise PromotionError("You have already used that code.", code="user_limit_reached")

        locked.used_count = locked.used_count + 1
        try:
            locked.save(update_fields=["used_count"])
        except IntegrityError as exc:  # pragma: no cover - only if the DB backstop trips
            raise PromotionError(
                "That code has reached its usage limit.", code="limit_reached"
            ) from exc

        try:
            return PromotionUsage.objects.create(
                promotion=locked,
                user=user,
                order=order,
                discount_amount=discount,
            )
        except IntegrityError as exc:
            raise PromotionError(
                "That promotion was already applied to this order.", code="already_used"
            ) from exc
