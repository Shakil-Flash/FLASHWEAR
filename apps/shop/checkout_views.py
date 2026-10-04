"""Checkout views.

Sign-in required throughout: checkout is built on the customer's address book, and
an anonymous checkout would have nowhere to deliver. Every POST redirects back to
the checkout page (PRG) so a refresh never re-submits.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts.models import Address
from apps.orders.models import Order
from apps.shop.checkout import (
    cart_matches_snapshot,
    get_or_create_checkout,
    set_shipping_address,
    set_shipping_method,
    validate_checkout,
)
from apps.shop.models import CheckoutSession
from apps.shop.services import get_cart_for_request, get_cart_totals, get_price_changes
from apps.shop.shipping import get_shipping_methods


def _require_cart(request):
    """The active cart, or None when there is nothing to check out."""
    return get_cart_for_request(request)


def _own_order(request, number: str):
    """The customer's own order, or 404. Order numbers are never resolved across users."""
    return get_object_or_404(Order, number=number, user=request.user)


def _addresses_for(user):
    """The customer's own addresses, default delivery first.

    Scoped to ``user`` in the query itself: an address id posted from another
    account resolves to 404 before any service sees it.
    """
    return Address.objects.filter(user=user).order_by("-is_default_shipping", "-updated_at")


@login_required
@never_cache
def checkout_detail(request):
    """``/shop/checkout/`` -- address, shipping method, review, validate."""
    cart = _require_cart(request)
    if cart is None or cart.is_empty():
        messages.info(request, _("Your bag is empty."))
        return redirect("shop:cart")

    checkout = get_or_create_checkout(request.user, cart)

    # Editing the bag after validation invalidates the frozen totals: reopen rather
    # than let a stale total be charged later.
    if checkout.is_validated and not cart_matches_snapshot(cart, checkout.snapshot):
        checkout.status = checkout.Status.OPEN
        checkout.validated_at = None
        checkout.snapshot = {}
        checkout.save(update_fields=["status", "validated_at", "snapshot", "updated_at"])

    totals = get_cart_totals(cart)
    subtotal = totals["subtotal"]
    methods = get_shipping_methods(subtotal)
    selected = next((m for m in methods if m.code == checkout.shipping_method_code), methods[0])
    total = subtotal + selected.amount

    context = {
        "cart": cart,
        "checkout": checkout,
        "totals": totals,
        "addresses": _addresses_for(request.user),
        "shipping_methods": methods,
        "selected_method": selected,
        "shipping_amount": selected.amount,
        "total": total,
        "errors": [] if checkout.is_validated else _cart_errors(cart),
        "price_changes": get_price_changes(cart),
        "snapshot": checkout.snapshot,
    }
    return render(request, "shop/checkout.html", context)


@login_required
@require_POST
def checkout_address(request):
    """Choose the delivery address for this checkout."""
    cart = _require_cart(request)
    if cart is None or cart.is_empty():
        return redirect("shop:cart")

    checkout = get_or_create_checkout(request.user, cart)
    address = get_object_or_404(Address, pk=request.POST.get("address_id"), user=request.user)
    set_shipping_address(checkout, address)
    return redirect("shop:checkout")


@login_required
@require_POST
def checkout_shipping(request):
    """Choose the shipping method for this checkout."""
    cart = _require_cart(request)
    if cart is None or cart.is_empty():
        return redirect("shop:cart")

    checkout = get_or_create_checkout(request.user, cart)
    try:
        set_shipping_method(checkout, request.POST.get("method", ""))
    except ValidationError as err:
        messages.error(request, _message_text(err))
        return redirect("shop:checkout")
    return redirect("shop:checkout")


@login_required
@require_POST
def checkout_validate(request):
    """Re-check the cart in a transaction and freeze the validated snapshot."""
    cart = _require_cart(request)
    if cart is None or cart.is_empty():
        messages.error(request, _("Your bag is empty."))
        return redirect("shop:cart")

    checkout = get_or_create_checkout(request.user, cart)
    try:
        validate_checkout(checkout)
    except ValidationError as err:
        messages.error(request, _message_text(err))
        return redirect("shop:checkout")

    messages.success(request, _("Everything checks out. Review below to continue."))
    return redirect("shop:checkout")


# =============================================================================
# Phase 6: order placement and payment
# =============================================================================


@login_required
@never_cache
def checkout_payment(request, number: str):
    """``/shop/checkout/payment/<number>/`` -- the payment step for one order.

    Unpaid, non-terminal state only: anything already decided (paid, failed,
    cancelled) is sent to the confirmation page, where the truth lives.
    """
    order = _own_order(request, number)
    payment = getattr(order, "payment", None)
    if payment is None:
        from apps.payments.services import start_payment

        payment = start_payment(order)

    if payment.status == payment.Status.SUCCEEDED or order.status != order.Status.PENDING_PAYMENT:
        return redirect("shop:checkout-done", number=order.number)

    from apps.payments.providers import provider_name

    return render(
        request,
        "shop/payment.html",
        {
            "order": order,
            "payment": payment,
            "items": order.items.all(),
            "is_development": provider_name() == "development",
        },
    )


@login_required
@never_cache
def checkout_done(request, number: str):
    """``/shop/checkout/done/<number>/`` -- order confirmation and status."""
    order = _own_order(request, number)
    return render(
        request,
        "shop/order_done.html",
        {
            "order": order,
            "payment": getattr(order, "payment", None),
            "items": order.items.all(),
            "events": list(order.events.all()[:12]),
        },
    )


@login_required
@require_POST
def checkout_place(request):
    """Place the order: validated checkout -> order -> payment attempt.

    Idempotent by construction: the handoff returns the existing order when the
    session is already converted, so a double-clicked button lands on the same
    payment step instead of a second order.
    """
    from apps.inventory.services import InsufficientStock
    from apps.orders.services import CheckoutNotReady, StaleCheckout, create_order_from_checkout
    from apps.payments.services import start_payment

    checkout = get_object_or_404(
        CheckoutSession, pk=request.POST.get("checkout_id"), user=request.user
    )
    try:
        order = create_order_from_checkout(checkout)
        start_payment(order)
    except CheckoutNotReady:
        messages.error(request, _("Please review your bag before placing the order."))
        return redirect("shop:checkout")
    except StaleCheckout as err:
        messages.error(request, " ".join(err.problems) or _("Your bag changed. Review it again."))
        return redirect("shop:checkout")
    except InsufficientStock as err:
        messages.error(
            request,
            _("We could not hold %(sku)s for you: only %(available)d left.")
            % {"sku": err.sku, "available": err.available},
        )
        return redirect("shop:checkout")

    return redirect("shop:checkout-payment", number=order.number)


# =============================================================================
# Helpers
# =============================================================================


def _cart_errors(cart) -> list[str]:
    from apps.shop.services import validate_cart_items

    return validate_cart_items(cart)


def _message_text(err: ValidationError) -> str:
    """Flatten a ValidationError (including message-lists) into one flash string."""
    if hasattr(err, "messages"):
        return " ".join(err.messages)
    return str(err)
