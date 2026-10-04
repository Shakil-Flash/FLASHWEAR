"""Checkout orchestration.

Checkout sits between the cart (Phase 5) and the order (Phase 6). This module owns
three jobs:

* keeping one open checkout session per customer;
* collecting the customer's choices -- address and shipping method -- without ever
  trusting a posted price;
* **validation**: re-reading the cart inside a transaction, re-checking every line
  against the live catalogue, and freezing a snapshot Phase 6 can build an order from.

Validation ends the Phase 5 boundary. There is no payment instrument here, no order
number, no stock reservation.
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
    "get_or_create_checkout",
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

    shipping = method.amount
    total = (Decimal(subtotal) + Decimal(shipping)).quantize(Decimal("0.01"))

    checkout.shipping_address = address
    checkout.shipping_method_code = method.code
    checkout.subtotal = subtotal
    checkout.shipping_amount = shipping
    checkout.total = total
    checkout.status = CheckoutSession.Status.VALIDATED
    checkout.validated_at = timezone.now()
    checkout.snapshot = _build_snapshot(cart, checkout, method, subtotal, shipping, total)
    checkout.save()
    return checkout


# =============================================================================
# Internals
# =============================================================================


def _current_subtotal(checkout: CheckoutSession) -> Decimal:
    return get_cart_totals(checkout.cart)["subtotal"]


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


def _build_snapshot(cart, checkout, method, subtotal, shipping, total) -> dict:
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
