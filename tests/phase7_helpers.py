"""Shared builders for the Phase 7 test modules (reviews, loyalty, promotions, discounts).

Everything customer-facing goes through the real services, exactly like Phase 6's helpers.
The two deliberate shortcuts, both marked as such at their call sites:

* :func:`grant_points` uses the admin adjustment service -- a starting balance *is* operator
  input, not history, so seeding it through the same service production uses is honest.
* :func:`make_review` writes a row directly. Eligibility is exercised end to end in its own
  tests; display and moderation tests need many published rows and would otherwise pay for a
  paid order per author just to prove a histogram adds up.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.engagement.models import Promotion, Review
from apps.engagement.services import loyalty as loyalty_services

__all__ = [
    "checkout_totals",
    "discounted_checkout",
    "grant_points",
    "make_promotion",
    "make_review",
    "pay_order",
    "review_payload",
]


def make_promotion(
    code: str = "SAVE10",
    *,
    percent: int | None = None,
    fixed: float | str | None = None,
    **overrides,
):
    """An active promotion with a sensible window. Exactly one of ``percent``/``fixed``."""
    assert (percent is None) ^ (fixed is None), "give percent XOR fixed"
    now = timezone.now()
    params = {
        "code": code,
        "name": overrides.pop("name", f"{code.title()} offer"),
        "discount_type": (
            Promotion.DiscountType.PERCENTAGE if percent is not None else Promotion.DiscountType.FIXED
        ),
        "discount_value": Decimal(str(percent if percent is not None else fixed)),
        "starts_at": now - timedelta(days=1),
        "ends_at": now + timedelta(days=7),
    }
    params.update(overrides)
    return Promotion.objects.create(**params)


def grant_points(user, amount: int, *, note: str = "Test grant"):
    """A starting balance through the real admin adjustment service."""
    return loyalty_services.adjust_balance(user, amount, note=note)


def make_review(
    order,
    user,
    product,
    *,
    rating: int = 5,
    title: str = "Solid piece",
    body: str = "Fits exactly as described and the fabric holds up.",
    status: str = Review.Status.PUBLISHED,
    verified: bool = True,
):
    """A review row written directly (display/moderation fixtures only -- see module docstring)."""
    return Review.objects.create(
        order=order,
        author=user,
        product=product,
        rating=rating,
        title=title,
        body=body,
        status=status,
        verified_purchase=verified,
        published_at=timezone.now() if status == Review.Status.PUBLISHED else None,
    )


def review_payload(**overrides):
    """A valid review submission payload, as the product page form posts it."""
    payload = {"rating": 5, "title": "Exactly my size", "body": "Wore it all week, no complaints."}
    payload.update(overrides)
    return payload


def checkout_totals(checkout) -> dict:
    """The frozen snapshot's money fields as Decimals, for precise assertions."""
    return {
        "subtotal": Decimal(str(checkout.snapshot["subtotal"])),
        "shipping": Decimal(str(checkout.snapshot["shipping"]["amount"])),
        "total": Decimal(str(checkout.snapshot["total"])),
        "promotion": Decimal(str((checkout.snapshot.get("promotion") or {}).get("discount", 0))),
        "loyalty": Decimal(str((checkout.snapshot.get("loyalty") or {}).get("discount", 0))),
        "points": int((checkout.snapshot.get("loyalty") or {}).get("points", 0)),
    }


def discounted_checkout(user, variant, *, quantity: int = 1, code=None, points: int = 0, stock: int | None = 10):
    """A validated checkout with a promotion code and/or points applied, through real services."""
    from apps.shop.checkout import (
        set_loyalty_points,
        set_promotion_code,
        validate_checkout,
    )
    from apps.shop.models import Cart, CartItem, CheckoutSession
    from tests.phase6_helpers import make_address, seed_stock

    if stock is not None:
        seed_stock(variant, stock)
    cart = Cart.objects.create(user=user)
    CartItem.objects.create(
        cart=cart, variant=variant, quantity=quantity, price_snapshot=variant.price
    )
    checkout = CheckoutSession.objects.create(
        user=user, cart=cart, shipping_address=make_address(user)
    )
    if code:
        set_promotion_code(checkout, code)
    if points:
        set_loyalty_points(checkout, points)
    return validate_checkout(checkout)


def pay_order(order):
    """Take an unpaid order through payment (webhook included), like Phase 6's place_and_pay."""
    from apps.payments.providers.development import make_provider_event
    from apps.payments.services import handle_provider_event, start_payment

    payment = start_payment(order)
    handle_provider_event("development", make_provider_event(payment, "succeeded"))
    order.refresh_from_db()  # the webhook moved a different in-memory instance
    return order
