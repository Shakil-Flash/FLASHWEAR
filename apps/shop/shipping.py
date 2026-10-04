"""Shipping methods for checkout.

A deliberately small abstraction: checkout asks "what are the options and what do
they cost for this subtotal?" and gets back value objects. Rates are settings, not
database rows -- a carrier integration (rates by weight, by zone, by real carrier
quotes) is a later phase behind this same interface.

Every amount is ``Decimal``. Nothing here touches stock or delivery tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings


@dataclass(frozen=True)
class ShippingMethod:
    """One selectable shipping option."""

    code: str
    name: str
    amount: Decimal
    estimate: str

    @property
    def is_free(self) -> bool:
        return self.amount == Decimal("0.00")


def _rates() -> tuple[Decimal, Decimal, Decimal]:
    flat = getattr(settings, "CHECKOUT_SHIPPING_FLAT_RATE", Decimal("5.00"))
    free_over = getattr(settings, "CHECKOUT_SHIPPING_FREE_OVER", Decimal("100.00"))
    express = getattr(settings, "CHECKOUT_SHIPPING_EXPRESS_RATE", Decimal("15.00"))
    return Decimal(flat), Decimal(free_over), Decimal(express)


def get_shipping_methods(subtotal: Decimal) -> list[ShippingMethod]:
    """Return every shipping method available for ``subtotal``.

    Standard delivery is free over the configured threshold; express always costs
    the express rate. Both are shown even when standard is free, because hiding the
    paid option would take away a choice the customer is entitled to make.
    """
    flat, free_over, express = _rates()
    standard_amount = Decimal("0.00") if subtotal >= free_over else flat
    return [
        ShippingMethod(
            code="standard",
            name="Standard delivery",
            amount=standard_amount,
            estimate="3-5 business days",
        ),
        ShippingMethod(
            code="express",
            name="Express delivery",
            amount=express,
            estimate="1-2 business days",
        ),
    ]


def get_shipping_method(code: str, subtotal: Decimal) -> ShippingMethod | None:
    """Resolve one method by code, or None when the code is unknown.

    Checkout must never trust a posted amount: the cost comes from here, keyed on
    the server's own subtotal.
    """
    for method in get_shipping_methods(subtotal):
        if method.code == code:
            return method
    return None
