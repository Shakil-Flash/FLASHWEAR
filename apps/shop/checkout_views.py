"""Checkout views.

Sign-in required throughout: checkout is built on the customer's address book, and
an anonymous checkout would have nowhere to deliver. Every POST redirects back to
the checkout page (PRG) so a refresh never re-submits.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts.models import Address
from apps.analytics.services import record_event
from apps.orders.models import Order
from apps.shop.checkout import (
    cart_matches_snapshot,
    discount_breakdown,
    get_or_create_checkout,
    set_loyalty_points,
    set_promotion_code,
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

    record_event(
        "checkout_started",
        request=request,
        object_type="checkout",
        object_id=checkout.pk,
        idempotency_key=f"checkout_started:{checkout.pk}",
    )

    # Editing the bag after validation invalidates the frozen totals: reopen rather
    # than let a stale total be charged later.
    if checkout.is_validated and not cart_matches_snapshot(cart, checkout.snapshot):
        checkout.status = checkout.Status.OPEN
        checkout.validated_at = None
        checkout.snapshot = {}
        checkout.save(update_fields=["status", "validated_at", "snapshot", "updated_at"])

    # The bag's rows are fetched once and shared by totals, validation, the price-change
    # diff and the template loop -- four consumers that used to query the same list.
    items = list(cart.get_items())
    totals = get_cart_totals(cart, items=items)
    subtotal = totals["subtotal"]
    methods = get_shipping_methods(subtotal)
    selected = next((m for m in methods if m.code == checkout.shipping_method_code), methods[0])

    # Phase 7: resolve the current discounts for the pre-validation estimate. An ineligible
    # code/points combination lands in ``errors`` (which also disables the validate button)
    # rather than silently pricing without it -- the customer sees *why* before continuing.
    errors = [] if checkout.is_validated else _cart_errors(cart, items=items)
    promo_discount = Decimal("0.00")
    loyalty_discount = Decimal("0.00")
    if not checkout.is_validated and (checkout.promotion_code or checkout.loyalty_points):
        try:
            breakdown = discount_breakdown(
                checkout,
                promotion_code=checkout.promotion_code,
                loyalty_points=checkout.loyalty_points,
            )
            promo_discount = breakdown.promotion_discount
            loyalty_discount = breakdown.loyalty_discount
        except ValidationError as err:
            errors.append(_message_text(err))

    total = (subtotal - promo_discount - loyalty_discount + selected.amount).quantize(
        Decimal("0.01")
    )
    if total < 0:  # pragma: no cover - each discount is capped at the subtotal
        total = Decimal("0.00")

    # Loyalty panel facts for the open checkout.
    from apps.engagement.services import loyalty as loyalty_services

    loyalty_balance = loyalty_services.balance_for(request.user)
    loyalty_max = loyalty_services.max_redeemable_points(
        request.user,
        eligible_subtotal=max(subtotal - promo_discount, Decimal("0.00")),
        balance=loyalty_balance,
    )

    context = {
        "cart": cart,
        "items": items,
        "checkout": checkout,
        "totals": totals,
        "addresses": _addresses_for(request.user),
        "shipping_methods": methods,
        "selected_method": selected,
        "shipping_amount": selected.amount,
        "total": total,
        "errors": errors,
        "price_changes": get_price_changes(cart, items=items),
        "snapshot": checkout.snapshot,
        "promo_discount": promo_discount,
        "loyalty_discount": loyalty_discount,
        "loyalty_balance": loyalty_balance,
        "loyalty_max": loyalty_max,
        "redemption_message": loyalty_services.redemption_message(request.user),
        "redeem_increment": settings.LOYALTY_REDEEM_INCREMENT,
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
    record_event(
        "checkout_step_completed",
        request=request,
        object_type="checkout",
        object_id=checkout.pk,
        metadata={"step": "address", "address_id": address.pk},
    )
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
        record_event(
            "checkout_step_completed",
            request=request,
            object_type="checkout",
            object_id=checkout.pk,
            metadata={"step": "shipping", "shipping_method": checkout.shipping_method_code},
        )
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
# Phase 7: promotion code and FLASH Points
# =============================================================================


@login_required
@require_POST
def checkout_promotion(request):
    """Apply (or, with an empty code, clear) the checkout's promotion code.

    The engine validates against the live subtotal before anything persists, so an invalid
    code never becomes state -- the old code simply stays and the reason flashes back.
    """
    cart = _require_cart(request)
    if cart is None or cart.is_empty():
        return redirect("shop:cart")

    checkout = get_or_create_checkout(request.user, cart)
    code = request.POST.get("code", "")
    try:
        set_promotion_code(checkout, code)
    except ValidationError as err:
        messages.error(request, _message_text(err))
        return redirect("shop:checkout")

    if code.strip():
        messages.success(
            request,
            _("Promotion %(code)s applied.") % {"code": checkout.promotion_code},
        )
    else:
        messages.info(request, _("Promotion removed."))
    return redirect("shop:checkout")


@login_required
@require_POST
def checkout_loyalty(request):
    """Apply or remove FLASH Points redemption for this checkout.

    Removal is explicit (a ``remove`` submit); applying validates amount, balance, caps and
    increment through the engine and pins the hold, all before the redirect.
    """
    cart = _require_cart(request)
    if cart is None or cart.is_empty():
        return redirect("shop:cart")

    checkout = get_or_create_checkout(request.user, cart)
    if request.POST.get("remove"):
        points = 0
    else:
        points = request.POST.get("points", 0) or 0
    try:
        set_loyalty_points(checkout, points)
    except ValidationError as err:
        messages.error(request, _message_text(err))
        return redirect("shop:checkout")

    if int(points or 0) > 0:
        messages.success(
            request,
            _("%(points)d FLASH Points will be applied at validation.")
            % {"points": checkout.loyalty_points},
        )
    else:
        messages.info(request, _("FLASH Points removed."))
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

    record_event(
        "payment_started",
        request=request,
        object_type="payment",
        object_id=payment.pk,
        metadata={"order_number": order.number, "amount": str(order.total)},
    )

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
@require_POST
def checkout_payment_retry(request, number: str):
    """Allow customer to retry a failed payment attempt without rebuilding the order."""
    order = _own_order(request, number)
    if order.status != order.Status.PENDING_PAYMENT:
        return redirect("shop:checkout-done", number=order.number)

    payment = getattr(order, "payment", None)
    if payment and payment.status == payment.Status.FAILED:
        from apps.payments.models import Payment
        from apps.payments.services import start_payment

        payment.transition_to(Payment.Status.CREATED)
        payment.failure_code = ""
        payment.failure_message = ""
        payment.save(update_fields=["status", "failure_code", "failure_message", "updated_at"])
        start_payment(order)
        messages.info(request, _("Ready to retry payment."))

    return redirect("shop:checkout-payment", number=order.number)


@login_required
@never_cache
def checkout_done(request, number: str):
    """``/shop/checkout/done/<number>/`` -- order confirmation and status."""
    order = _own_order(request, number)

    if order.status in {
        order.Status.PAID,
        order.Status.PROCESSING,
        order.Status.SHIPPED,
        order.Status.DELIVERED,
    }:
        record_event(
            "order_completed",
            request=request,
            object_type="order",
            object_id=order.pk,
            idempotency_key=f"order_completed:{order.pk}",
            metadata={"order_number": order.number, "total": str(order.total)},
        )

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

    record_event(
        "checkout_completed",
        request=request,
        object_type="order",
        object_id=order.pk,
        idempotency_key=f"checkout_completed:{order.pk}",
    )

    return redirect("shop:checkout-payment", number=order.number)


# =============================================================================
# Helpers
# =============================================================================


def _cart_errors(cart, *, items=None) -> list[str]:
    from apps.shop.services import validate_cart_items

    return validate_cart_items(cart, items=items)


def _message_text(err: ValidationError) -> str:
    """Flatten a ValidationError (including message-lists) into one flash string."""
    if hasattr(err, "messages"):
        return " ".join(err.messages)
    return str(err)
