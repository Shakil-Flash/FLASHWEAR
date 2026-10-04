"""Shared builders for the Phase 6 test modules (inventory, orders, payments).

Everything here goes through the real services -- a helper that hand-rolled an
order would let a test pass against a state production can never reach. The one
deliberate shortcut is :func:`seed_stock`, which sets stock counters directly:
fixtures are not history, so tests that care about the ledger start from zero
and use :func:`apps.inventory.services.adjust_stock` instead.
"""

from __future__ import annotations

import json

from django.urls import reverse

from apps.accounts.models import Address
from apps.inventory.models import Stock
from apps.payments.providers.development import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    build_event,
    sign_payload,
)
from apps.shop.checkout import validate_checkout
from apps.shop.models import Cart, CartItem, CheckoutSession

__all__ = [
    "add_to_bag",
    "make_address",
    "message_texts",
    "open_checkout",
    "place_and_pay",
    "placed_order",
    "seed_stock",
    "sign_in",
    "signed_headers",
    "validated_checkout",
    "webhook_body",
]


def sign_in(client, email="shopper@flashwear.test"):
    """Real login POST, so the ``user_logged_in`` merge signal actually runs."""
    return client.post(
        reverse("accounts:login"),
        {"username": email, "password": "Str0ng-Passw0rd!"},
    )


def add_to_bag(client, variant, quantity=1):
    """POST a variant into the bag the way the product page form does."""
    return client.post(
        reverse("shop:cart-add"),
        {"variant_id": variant.pk, "quantity": quantity},
    )


def message_texts(response) -> str:
    return " | ".join(str(message) for message in response.context.get("messages", []))


def make_address(user, **overrides):
    params = {
        "user": user,
        "full_name": "Ada Lovelace",
        "phone": "+44 7700 900222",
        "line1": "1 Example Way",
        "line2": "Apartment 3",
        "city": "London",
        "region": "Greater London",
        "postal_code": "SW1A 1AA",
        "country": "GB",
        "address_type": "both",
        "is_default_shipping": False,
    }
    params.update(overrides)
    return Address.objects.create(**params)


def seed_stock(variant, on_hand: int, reserved: int = 0):
    """Set a variant's counters directly (fixtures are not ledger history)."""
    stock = Stock.get_for_variant(variant)
    stock.on_hand = on_hand
    stock.reserved = reserved
    stock.save(update_fields=["on_hand", "reserved", "updated_at"])
    return stock


def open_checkout(user, *, variant=None, quantity=1):
    """An ``OPEN`` checkout session with (optionally) one cart line."""
    cart = Cart.objects.create(user=user)
    if variant is not None:
        CartItem.objects.create(
            cart=cart, variant=variant, quantity=quantity, price_snapshot=variant.price
        )
    return CheckoutSession.objects.create(user=user, cart=cart)


def validated_checkout(user, variant, quantity=1, *, stock: int | None = None):
    """A cart through the real validation step: the handoff's happy-path input."""
    if stock is not None:
        seed_stock(variant, stock)
    cart = Cart.objects.create(user=user)
    CartItem.objects.create(
        cart=cart, variant=variant, quantity=quantity, price_snapshot=variant.price
    )
    checkout = CheckoutSession.objects.create(
        user=user, cart=cart, shipping_address=make_address(user)
    )
    return validate_checkout(checkout)


def placed_order(user, variant, quantity=1, *, stock: int | None = 10):
    """Validated checkout -> order, through the real handoff.

    ``stock=None`` leaves the counters alone (for a second order in the same
    test, where re-seeding would clobber the first order's active hold).
    """
    from apps.orders.services import create_order_from_checkout

    return create_order_from_checkout(validated_checkout(user, variant, quantity, stock=stock))


def place_and_pay(user, variant, quantity=1, *, stock: int | None = 10):
    """An order paid through the real services (webhook included)."""
    from apps.payments.providers.development import make_provider_event
    from apps.payments.services import handle_provider_event, start_payment

    order = placed_order(user, variant, quantity, stock=stock)
    payment = start_payment(order)
    handle_provider_event("development", make_provider_event(payment, "succeeded"))
    order.refresh_from_db()  # the webhook moved a different in-memory instance
    return order


def webhook_body(
    payment,
    event_type="succeeded",
    *,
    event_id=None,
    amount=None,
    reference=None,
    reason="",
):
    """The provider's JSON bytes for ``payment`` (optionally tampered with)."""
    body = build_event(payment, event_type, reason=reason)
    if event_id is not None:
        body["event_id"] = event_id
    if amount is not None:
        body["amount"] = str(amount)
    if reference is not None:
        body["reference"] = reference
    return json.dumps(body).encode()


def signed_headers(raw: bytes, *, secret=None, timestamp=None):
    """The exact headers a client must send for ``raw``."""
    stamp, signature = sign_payload(raw, secret=secret, timestamp=timestamp)
    return {TIMESTAMP_HEADER: stamp, SIGNATURE_HEADER: signature}
