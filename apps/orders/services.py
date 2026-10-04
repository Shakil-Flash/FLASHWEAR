"""Checkout -> order handoff and the order/shipment lifecycle.

``create_order_from_checkout`` is the single door between Phase 5 and Phase 6:

* **Idempotent.** The checkout row is locked first; a session that is already
  ``CONVERTED`` returns the order that was built from it, so a double-clicked
  "Place order" cannot create two orders. ``Order.checkout`` being a one-to-one
  makes a second order structurally impossible, not merely unlikely.
* **Revalidating.** The frozen snapshot is checked against the live cart and the
  live catalogue (prices included) before anything is written, because charging a
  stale total would be the worst bug this phase could ship.
* **Reserving first.** Stock holds are taken inside the same transaction; if the
  units are gone, :class:`~apps.inventory.services.InsufficientStock` rolls the
  whole thing back and the customer keeps their validated checkout.

Lock order in this module: checkout -> stock rows (ascending variant id) -> promotion
row (consumption only). Stock is taken before the promotion is locked, and
:func:`apps.engagement.services.promotions.consume_usage` is the *only* code that locks a
promotion, so no path can take a promotion first and create a cycle. Validation of discounts
never locks anything -- only consumption does.
"""

from __future__ import annotations

import secrets
import string
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.inventory.services import (
    attach_holds_to_order,
    release_holds,
    reserve_stock,
)
from apps.orders.models import (
    InvalidTransition,
    Order,
    OrderAddress,
    OrderEvent,
    OrderItem,
    Shipment,
)
from apps.orders.signals import order_cancelled
from apps.shop.checkout import cart_matches_snapshot
from apps.shop.models import CheckoutSession
from apps.shop.services import validate_cart_items

__all__ = [
    "CheckoutNotReady",
    "StaleCheckout",
    "cancel_order",
    "create_order_from_checkout",
    "deliver_order",
    "generate_order_number",
    "mark_processing",
    "ship_order",
]


class CheckoutNotReady(Exception):
    """The session is not in a state an order can be built from."""


class StaleCheckout(Exception):
    """The bag or the catalogue moved since validation; totals may be wrong."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__(" ".join(self.problems))


def generate_order_number() -> str:
    """``FW-<yyyymmdd>-<8 random alphanumerics>``.

    Date-stamped for humans, random-suffixed so numbers carry no ordering
    information (an order id is not a counter an attacker can walk).
    """
    stamp = timezone.now().strftime("%Y%m%d")
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"FW-{stamp}-{suffix}"


# =============================================================================
# Handoff
# =============================================================================


@transaction.atomic
def create_order_from_checkout(checkout: CheckoutSession) -> Order:
    """Build (or return) the order for a validated checkout session.

    Raises:
        CheckoutNotReady: session is not ``VALIDATED``.
        StaleCheckout: the bag or a price changed since validation.
        InsufficientStock: reserved units are gone; nothing is written.
    """
    checkout = CheckoutSession.objects.select_for_update().get(pk=checkout.pk)

    # Idempotency first: a replayed submit finds the order already there.
    existing = Order.objects.filter(checkout=checkout).first()
    if existing is not None:
        return existing
    if checkout.status != CheckoutSession.Status.VALIDATED or not checkout.snapshot:
        raise CheckoutNotReady(
            "Checkout is not validated; validate the bag before placing the order."
        )

    snapshot = checkout.snapshot
    cart = checkout.cart
    _assert_snapshot_matches_cart(cart, snapshot)
    _assert_snapshot_prices_current(snapshot)
    lines = [
        {"variant_id": line["variant_id"], "quantity": line["quantity"]}
        for line in snapshot["lines"]
    ]
    if not lines:
        raise CheckoutNotReady("Validated checkout has no lines.")

    number = generate_order_number()
    reference = f"order:{number}"

    # Phase 7: the frozen discounts must still be true. The engine re-checks the promotion
    # window/limits and the spendable balance against the live world; anything that moved
    # (window closed, last use spent, hold expired) fails the whole handoff instead of
    # charging a total the customer never agreed to.
    promo_payload = snapshot.get("promotion") or {}
    loyalty_payload = snapshot.get("loyalty") or {}
    frozen_promo = Decimal(str(promo_payload.get("discount", "0") or "0"))
    frozen_loyalty = Decimal(str(loyalty_payload.get("discount", "0") or "0"))
    frozen_points = int(loyalty_payload.get("points", 0) or 0)

    subtotal = Decimal(snapshot["subtotal"])
    shipping = Decimal(snapshot["shipping"]["amount"])
    total = Decimal(snapshot["total"])
    live = _revalidate_discounts(
        checkout, subtotal, promo_payload.get("code", ""), frozen_points
    )
    if live.promotion_discount != frozen_promo or live.loyalty_discount != frozen_loyalty:
        raise StaleCheckout(
            ["Your discounts changed since the review step. Please review your order again."]
        )
    if total != subtotal - live.total_discount + shipping:
        raise StaleCheckout(["Totals do not add up; please review your bag again."])

    # Raises InsufficientStock -> transaction rolls back, hold state untouched.
    reserve_stock(checkout, lines, reference=reference)

    order = Order.objects.create(
        number=number,
        user=checkout.user,
        checkout=checkout,
        currency=snapshot.get("currency") or cart.currency,
        subtotal=subtotal,
        shipping_amount=shipping,
        discount_amount=live.total_discount,
        promotion_code=live.promotion_code,
        loyalty_points=live.points,
        total=total,
        shipping_code=snapshot["shipping"]["code"],
        shipping_name=snapshot["shipping"]["name"],
        shipping_estimate=snapshot["shipping"].get("estimate", ""),
    )
    OrderItem.objects.bulk_create(
        [
            OrderItem(
                order=order,
                variant_id=line["variant_id"],
                sku=line["sku"],
                product_name=line["product"],
                option_label=line["option"],
                quantity=line["quantity"],
                unit_price=Decimal(line["unit_price"]),
                line_total=Decimal(line["line_total"]),
            )
            for line in snapshot["lines"]
        ]
    )
    OrderAddress.objects.create(order=order, **_address_values(checkout, snapshot))

    OrderEvent.objects.create(
        order=order,
        event_type=OrderEvent.Type.CREATED,
        note="Order placed.",
        metadata={
            "checkout": checkout.pk,
            "shipping_code": order.shipping_code,
            "total": str(order.total),
        },
    )
    OrderEvent.objects.create(
        order=order,
        event_type=OrderEvent.Type.STOCK_RESERVED,
        note="Stock held for payment.",
        metadata={"lines": len(lines)},
    )

    attach_holds_to_order(checkout, order)

    # Phase 7: charge the promotion (usage row + counter, under the promotion row lock) and
    # move the points hold onto the order. Both are conditional/idempotent, so a replayed
    # submit cannot double-count either one.
    _consume_promotion(live, checkout, order)
    if frozen_points:
        from apps.engagement.models import PointsReservation

        attached = PointsReservation.objects.filter(
            checkout=checkout,
            status=PointsReservation.Status.ACTIVE,
            points=frozen_points,
        ).update(order=order)
        if not attached:
            raise StaleCheckout(
                ["Your FLASH Points need to be applied again. Please review your order."]
            )

    checkout.status = CheckoutSession.Status.CONVERTED
    checkout.save(update_fields=["status", "updated_at"])
    _convert_cart(cart)
    return order


def _revalidate_discounts(
    checkout: CheckoutSession, subtotal: Decimal, promotion_code: str, points: int
):
    """Re-run the engagement discount engine against the live world at handoff time.

    Read-only (no promotion lock, no counter change); consumption happens later in
    :func:`_consume_promotion`. Any rule violation becomes a ``StaleCheckout`` so the
    customer is sent back to review rather than charged on stale assumptions.
    """
    from apps.engagement.services.discounts import compute_discounts
    from apps.engagement.services.errors import EngagementError

    try:
        return compute_discounts(
            user=checkout.user,
            subtotal=subtotal,
            promotion_code=promotion_code,
            loyalty_points=points,
            excluding_checkout=checkout,
        )
    except EngagementError as exc:
        raise StaleCheckout(
            [f"{exc.message} Please review your order again."]
        ) from exc


def _consume_promotion(live, checkout: CheckoutSession, order: Order) -> None:
    """Write the usage row + counter for a promotion-carrying order, exactly once."""
    if live.promotion is None:
        return
    from apps.engagement.services.errors import EngagementError
    from apps.engagement.services.promotions import consume_usage

    try:
        consume_usage(
            live.promotion,
            user=checkout.user,
            order=order,
            discount=live.promotion_discount,
        )
    except EngagementError as exc:
        raise StaleCheckout(
            [f"{exc.message} Please review your order again."]
        ) from exc


def _convert_cart(cart) -> None:
    """Retire the bag the order came from.

    The cart row keeps its lines as a record of what was bought (Phase 5 gave it
    a ``CONVERTED`` state precisely for this); the unique "one active cart per
    customer" constraint only covers ``ACTIVE`` rows, so the next visit simply
    starts a fresh bag.
    """
    if cart.status != cart.Status.CONVERTED:
        cart.status = cart.Status.CONVERTED
        cart.converted_at = timezone.now()
        cart.save(update_fields=["status", "converted_at", "updated_at"])


def _assert_snapshot_matches_cart(cart, snapshot: dict) -> None:
    """The bag must still be what was validated (same variants, quantities, prices)."""
    if not cart_matches_snapshot(cart, snapshot):
        raise StaleCheckout(["Your bag changed after the review step. Please review it again."])
    problems = validate_cart_items(cart)
    if problems:
        raise StaleCheckout(problems)


def _assert_snapshot_prices_current(snapshot: dict) -> None:
    """Re-read every price from the catalogue; a silent increase is never charged."""
    from apps.catalog.models import ProductVariant

    variant_ids = [line["variant_id"] for line in snapshot["lines"]]
    variants = ProductVariant.objects.filter(pk__in=variant_ids).select_related("product")
    found = {variant.pk: variant for variant in variants}
    problems: list[str] = []
    for line in snapshot["lines"]:
        variant = found.get(line["variant_id"])
        if variant is None:
            problems.append(f"{line['sku']} is no longer available.")
            continue
        if variant.price != Decimal(line["unit_price"]):
            problems.append(f"The price of {line['product']} changed to {variant.price}.")
        if not variant.is_active or not variant.product.is_published:
            problems.append(f"{line['product']} is no longer available.")
    if problems:
        raise StaleCheckout(problems)


def _address_values(checkout: CheckoutSession, snapshot: dict) -> dict:
    """Snapshot text from the frozen payload; FK only if the entry still exists."""
    from apps.accounts.models import Address

    payload = snapshot.get("address") or {}
    values = {
        "full_name": payload.get("full_name", ""),
        "phone": payload.get("phone", ""),
        "line1": payload.get("line1", ""),
        "line2": payload.get("line2", ""),
        "city": payload.get("city", ""),
        "region": payload.get("region", ""),
        "postal_code": payload.get("postal_code", ""),
        "country": payload.get("country", ""),
    }
    address_id = payload.get("id")
    if address_id and Address.objects.filter(pk=address_id, user=checkout.user).exists():
        values["address_id"] = address_id
    return values


# =============================================================================
# Fulfilment
# =============================================================================


@transaction.atomic
def mark_processing(order: Order, *, actor=None, carrier: str = "") -> Shipment:
    """Paid -> processing, and open the (single) shipment for the order.

    ``@transaction.atomic`` is required, not an optimisation: ``select_for_update``
    raises outside a transaction on PostgreSQL, and the shipment row plus the order
    transition must land together (this is the entry point admin actions use).
    """
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status != Order.Status.PAID:
        raise InvalidTransition("order", order.status, Order.Status.PROCESSING)
    order.transition_to(Order.Status.PROCESSING, actor=actor, note="Fulfilment started.")

    shipment = Shipment.objects.create(
        order=order, method=order.shipping_code, carrier=carrier, status=Shipment.Status.PENDING
    )
    OrderEvent.objects.create(
        order=order,
        event_type=OrderEvent.Type.SHIPMENT_CREATED,
        actor=actor,
        note="Shipment opened.",
        metadata={"shipment": shipment.pk},
    )
    shipment.transition_to(Shipment.Status.PROCESSING, actor=actor, note="Picking started.")
    return shipment


def _current_shipment(order: Order) -> Shipment:
    shipment = (
        order.shipments.exclude(status=Shipment.Status.CANCELLED).order_by("-created_at").first()
    )
    if shipment is None:
        raise InvalidTransition("shipment", "none", Shipment.Status.SHIPPED)
    return shipment


@transaction.atomic
def ship_order(
    order: Order, *, actor=None, tracking_number: str = "", carrier: str = ""
) -> Shipment:
    """Hand the parcel to the carrier; the order follows to ``SHIPPED``."""
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status == Order.Status.PAID:
        # Convenience: shipping a paid order starts processing first.
        return mark_processing(order, actor=actor, carrier=carrier).transition_to(
            Shipment.Status.SHIPPED,
            actor=actor,
            note="Handed to the carrier.",
            metadata={"tracking_number": tracking_number} if tracking_number else None,
        )
    if order.status != Order.Status.PROCESSING:
        raise InvalidTransition("order", order.status, Order.Status.SHIPPED)

    shipment = _current_shipment(order)
    if tracking_number:
        shipment.tracking_number = tracking_number
    if carrier:
        shipment.carrier = carrier
    shipment.save()
    return shipment.transition_to(
        Shipment.Status.SHIPPED,
        actor=actor,
        note="Handed to the carrier.",
        metadata={"tracking_number": tracking_number} if tracking_number else None,
    )


@transaction.atomic
def deliver_order(order: Order, *, actor=None) -> Shipment:
    """Mark the parcel delivered; the order follows to ``DELIVERED``."""
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status != Order.Status.SHIPPED:
        raise InvalidTransition("order", order.status, Order.Status.DELIVERED)
    shipment = _current_shipment(order)
    return shipment.transition_to(Shipment.Status.DELIVERED, actor=actor, note="Delivered.")


@transaction.atomic
def cancel_order(
    order: Order, *, actor=None, note: str = "", event_type: str = OrderEvent.Type.NOTE
) -> Order:
    """Cancel an *unpaid* order and give its held stock back.

    Paid orders are deliberately not cancellable here: cancelling one implies
    returning money, which needs the refund workflow (a later phase).

    Raises:
        InvalidTransition: the order is not ``PENDING_PAYMENT``.
    """
    order = Order.objects.select_for_update().get(pk=order.pk)
    if not order.is_cancellable:
        raise InvalidTransition("order", order.status, Order.Status.CANCELLED)

    released = release_holds(
        order.reservations.all(),
        reference=f"order:{order.number}",
        note="Order cancelled.",
    )
    order.transition_to(
        Order.Status.CANCELLED, actor=actor, note=note or "Order cancelled.", event_type=event_type
    )
    OrderEvent.objects.create(
        order=order,
        event_type=OrderEvent.Type.STOCK_RELEASED,
        actor=actor,
        note="Held stock returned to the pool.",
        metadata={"released": released},
    )
    # Same transaction as the transition: listeners (FLASH Points release) must observe the
    # cancellation or not at all.
    order_cancelled.send(sender=Order, order=order)
    return order
