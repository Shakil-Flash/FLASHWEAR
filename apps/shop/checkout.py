"""Checkout orchestration.

Checkout sits between the cart (Phase 5) and the order (Phase 6). This module owns
four jobs:

* keeping one open checkout session per customer;
* collecting the customer's choices -- address, shipping method, promotion code and
  FLASH Points -- without ever trusting a posted price;
* **validation**: re-reading the cart inside a transaction, re-checking every line
  against the live catalogue, resolving discounts through the engagement engine, and
  freezing a snapshot Phase 6 can build an order from;
* keeping the customer's points reservation in step (applied -> pinned, cleared ->
  released).

Validation ends the Phase 5 boundary. There is no payment instrument here, no order
number, no stock reservation. Engagement is reached through lazy imports so this module
stays importable whether or not Phase 7 services are loaded.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.shop.models import Cart, CheckoutSession
from apps.shop.services import (
    get_cart_totals,
    recalculate_price_snapshots,
    validate_cart_items,
)
from apps.shop.shipping import get_shipping_method

__all__ = [
    "cart_matches_snapshot",
    "discount_breakdown",
    "get_or_create_checkout",
    "set_loyalty_points",
    "set_promotion_code",
    "set_shipping_address",
    "set_shipping_method",
    "validate_checkout",
]


def get_or_create_checkout(user, cart: Cart) -> CheckoutSession:
    """Return the customer's checkout for ``cart``, creating it if needed.

    Reuses both OPEN and VALIDATED sessions: a validated session must be the one
    the checkout page reopens after the bag is edited, not a fresh sibling that
    would sit next to it forever.
    """
    checkout = (
        CheckoutSession.objects.filter(
            user=user,
            status__in=[
                CheckoutSession.Status.OPEN,
                CheckoutSession.Status.VALIDATED,
            ],
        )
        .order_by("-created_at")
        .first()
    )
    if checkout is None:
        return CheckoutSession.objects.create(
            user=user, cart=cart, status=CheckoutSession.Status.OPEN
        )
    # A cart swap (cleared bag, new session) must follow the checkout along.
    if checkout.cart_id != cart.pk:
        checkout.cart = cart
        checkout.save(update_fields=["cart", "updated_at"])
    return checkout


def set_shipping_address(checkout: CheckoutSession, address) -> CheckoutSession:
    """Choose the delivery address.

    Ownership is enforced here as well as in the view: an id that belongs to someone
    else is a validation error, never a silent assignment.
    """
    if address.user_id != checkout.user_id:
        raise ValidationError(_("That address is not yours."), code="address_forbidden")

    checkout.shipping_address = address
    _reopen_if_stale(checkout)
    checkout.save(update_fields=["shipping_address", "status", "validated_at", "updated_at"])
    return checkout


def set_shipping_method(checkout: CheckoutSession, code: str) -> CheckoutSession:
    """Choose a shipping method by its server-side code (never by posted price)."""
    subtotal = _current_subtotal(checkout)
    method = get_shipping_method(code, subtotal)
    if method is None:
        raise ValidationError(_("Unknown shipping method."), code="unknown_method")

    checkout.shipping_method_code = method.code
    _reopen_if_stale(checkout)
    checkout.save(update_fields=["shipping_method_code", "status", "validated_at", "updated_at"])
    return checkout


def set_promotion_code(checkout: CheckoutSession, code: str) -> CheckoutSession:
    """Apply (or clear, with an empty code) a promotion.

    The engine validates the *new* code against the live subtotal before anything persists,
    so an invalid code never becomes state -- the old code simply stays. Flash Points already
    on the checkout are part of that validation: a promotion that forbids them fails the apply
    and the customer removes them first, rather than silently dropping their points.
    """
    breakdown = discount_breakdown(
        checkout, promotion_code=code or "", loyalty_points=checkout.loyalty_points
    )
    checkout.promotion_code = breakdown.promotion_code
    _reopen_if_stale(checkout)
    checkout.save(update_fields=["promotion_code", "status", "validated_at", "updated_at"])
    return checkout


def set_loyalty_points(checkout: CheckoutSession, points) -> CheckoutSession:
    """Apply (or clear, with 0) FLASH Points redemption for this checkout.

    Validation and pinning happen together: the amount is checked against the balance (minus
    this checkout's own hold), the caps and the increment grid, then the reservation is
    (re)taken with a fresh expiry. ``0`` releases the hold.
    """
    breakdown = discount_breakdown(
        checkout, promotion_code=checkout.promotion_code, loyalty_points=points or 0
    )
    checkout.loyalty_points = breakdown.points
    _reopen_if_stale(checkout)
    checkout.save(update_fields=["loyalty_points", "status", "validated_at", "updated_at"])
    _reserve_points(checkout, breakdown.points)
    return checkout


@transaction.atomic
def validate_checkout(checkout: CheckoutSession) -> CheckoutSession:
    """Re-check the cart and freeze a validated snapshot.

    The cart and its rows are locked for the duration, so two concurrent validates
    cannot both win, and a price edited mid-flight is seen exactly once.

    Raises:
        ValidationError: empty cart, invalid lines, or no address on file.
    """
    cart = Cart.objects.select_for_update().get(pk=checkout.cart_id)

    if cart.is_empty():
        raise ValidationError(_("Your bag is empty."), code="cart_empty")

    errors = validate_cart_items(cart)
    if errors:
        raise ValidationError(errors, code="cart_invalid")

    # Snapshots follow the live price so validation and display agree; totals are
    # computed from authoritative variant prices either way.
    recalculate_price_snapshots(cart)
    subtotal = get_cart_totals(cart)["subtotal"]

    address = checkout.shipping_address or _default_shipping_address(checkout.user)
    if address is None:
        raise ValidationError(_("Choose where we should deliver."), code="address_required")

    method = get_shipping_method(checkout.shipping_method_code, subtotal)
    if method is None:
        method = get_shipping_method("standard", subtotal)

    # Phase 7: promotions and FLASH Points ride along with the frozen snapshot. The engine
    # re-reads eligibility and the spendable balance (excluding this checkout's own hold),
    # so a code that expired or points that moved between steps fail here, not at payment.
    discounts = discount_breakdown(
        checkout,
        promotion_code=checkout.promotion_code,
        loyalty_points=checkout.loyalty_points,
    )

    shipping = method.amount
    total = (Decimal(subtotal) - discounts.total_discount + Decimal(shipping)).quantize(
        Decimal("0.01")
    )
    if total < 0:  # pragma: no cover - discounts are individually capped at the subtotal
        total = Decimal("0.00")

    # Pin the points with a fresh expiry now that validation proved the balance again.
    _reserve_points(checkout, discounts.points)

    checkout.shipping_address = address
    checkout.shipping_method_code = method.code
    checkout.subtotal = subtotal
    checkout.shipping_amount = shipping
    checkout.total = total
    checkout.status = CheckoutSession.Status.VALIDATED
    checkout.validated_at = timezone.now()
    checkout.snapshot = _build_snapshot(
        cart, checkout, method, subtotal, shipping, total, discounts
    )
    checkout.save()
    return checkout


# =============================================================================
# Internals
# =============================================================================


def _current_subtotal(checkout: CheckoutSession) -> Decimal:
    return get_cart_totals(checkout.cart)["subtotal"]


def discount_breakdown(checkout: CheckoutSession, *, promotion_code, loyalty_points):
    """Run the engagement discount engine, translating domain errors to form errors.

    Engagement is imported lazily (the shop must not depend on it at import time) and
    ``excluding_checkout`` lets this checkout see its own hold as free money, which is what
    makes re-validating an already-reserved checkout work against itself.
    """
    from apps.engagement.services.discounts import compute_discounts
    from apps.engagement.services.errors import EngagementError

    try:
        return compute_discounts(
            user=checkout.user,
            subtotal=_current_subtotal(checkout),
            promotion_code=promotion_code,
            loyalty_points=loyalty_points,
            excluding_checkout=checkout,
        )
    except EngagementError as exc:
        raise ValidationError(exc.message, code=exc.code) from exc


def _reserve_points(checkout: CheckoutSession, points: int) -> None:
    """Pin (or release, at 0) the checkout's points; same error translation as
    :func:`discount_breakdown`."""
    from apps.engagement.services.errors import EngagementError
    from apps.engagement.services.loyalty import reserve_for_checkout

    try:
        reserve_for_checkout(checkout, points)
    except EngagementError as exc:
        raise ValidationError(exc.message, code=exc.code) from exc


def _default_shipping_address(user):
    from apps.accounts.models import Address

    return (
        Address.objects.filter(user=user, is_default_shipping=True).order_by("-updated_at").first()
    )


def _reopen_if_stale(checkout: CheckoutSession) -> None:
    """Re-open a validated checkout whose choices no longer match the cart."""
    if checkout.status == CheckoutSession.Status.VALIDATED:
        checkout.status = CheckoutSession.Status.OPEN
        checkout.validated_at = None


def _address_payload(address) -> dict:
    return {
        "id": address.pk,
        "full_name": address.full_name,
        "phone": address.phone,
        "line1": address.line1,
        "line2": address.line2,
        "city": address.city,
        "region": address.region,
        "postal_code": address.postal_code,
        "country": address.country,
    }


def _build_snapshot(cart, checkout, method, subtotal, shipping, total, discounts) -> dict:
    """Freeze the validated state Phase 6 will build an order from."""
    lines = []
    for item in cart.get_items():
        lines.append(
            {
                "variant_id": item.variant_id,
                "sku": item.variant.sku,
                "product": item.variant.product.name,
                "option": item.variant.option_label,
                "quantity": item.quantity,
                "unit_price": str(item.price_snapshot),
                "line_total": str(item.line_total),
            }
        )
    return {
        "cart_id": cart.pk,
        "currency": cart.currency,
        "lines": lines,
        "subtotal": str(subtotal),
        "shipping": {
            "code": method.code,
            "name": method.name,
            "estimate": method.estimate,
            "amount": str(shipping),
        },
        # Phase 7 discounts, frozen alongside the lines. The handoff re-validates both
        # against the live engine and refuses to build an order if they moved.
        "promotion": (
            {
                "code": discounts.promotion_code,
                "discount": str(discounts.promotion_discount),
            }
            if discounts.promotion_code
            else None
        ),
        "loyalty": (
            {"points": discounts.points, "discount": str(discounts.loyalty_discount)}
            if discounts.points
            else None
        ),
        "total": str(total),
        "address": _address_payload(checkout.shipping_address),
    }


def cart_matches_snapshot(cart: Cart, snapshot: dict) -> bool:
    """True when the cart still holds exactly what was validated.

    Used to re-open a validated checkout after the customer edits the bag: charging
    a stale total would be the single worst bug this phase could ship.
    """
    if not snapshot or snapshot.get("cart_id") != cart.pk:
        return False

    current = [
        (item.variant_id, item.quantity, str(item.price_snapshot)) for item in cart.get_items()
    ]
    frozen = [
        (line["variant_id"], line["quantity"], line["unit_price"])
        for line in snapshot.get("lines", [])
    ]
    return current == frozen
