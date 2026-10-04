"""Stock mutation services.

Three rules keep the counters honest:

1. **Stock rows are locked last.** Every path first locks its own domain rows
   (checkout, order, payment) and only then takes stock locks, in ascending
   variant-id order. No transaction ever holds a stock lock while waiting on
   another row, so the lock graph has no cycles.
2. **Transitions are conditional.** A hold moves ``ACTIVE -> RELEASED / EXPIRED /
   CONSUMED`` through ``UPDATE ... WHERE status = 'active'``; only the caller that
   wins the row (rowcount 1) touches the counters. A replayed webhook or a double
   sweeper pass therefore cannot double-count.
3. **No money here.** Inventory counts units; prices belong to :mod:`apps.shop`
   and :mod:`apps.orders`.

Every public function opens its own transaction, so it is safe to call from a
view, a task or another service without threading ``atomic`` through the stack.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.inventory.models import InventoryMovement, Reservation, Stock

if TYPE_CHECKING:
    from apps.orders.models import Order
    from apps.shop.models import CheckoutSession

__all__ = [
    "InsufficientStock",
    "adjust_stock",
    "attach_holds_to_order",
    "consume_holds",
    "release_holds",
    "reserve_stock",
    "sweep_expired_holds",
]


class InsufficientStock(Exception):
    """Not enough available units for the requested quantity."""

    def __init__(self, sku: str, requested: int, available: int, *, idempotent: bool = False):
        self.sku = sku
        self.requested = requested
        self.available = available
        self.idempotent = idempotent
        super().__init__(
            f"{sku}: requested {requested}, {available} available"
            + (" (stock was already sold out)" if idempotent else "")
        )


def _lock_stock_rows(variant_ids) -> dict[int, Stock]:
    """Lock (and return) stock rows for ``variant_ids`` in ascending id order.

    ``sorted()`` is the whole point: two transactions reserving the same two
    variants must take the locks in the same sequence or they can deadlock.
    """
    ids = sorted(set(variant_ids))
    if not ids:
        return {}
    rows = Stock.objects.select_for_update().filter(variant_id__in=ids)
    return {row.variant_id: row for row in rows}


def _hold_expiry() -> datetime:
    return timezone.now() + timedelta(minutes=settings.INVENTORY_RESERVATION_MINUTES)


# =============================================================================
# Reserving
# =============================================================================


@transaction.atomic
def reserve_stock(
    checkout: CheckoutSession,
    lines: list[dict],
    *,
    reference: str = "",
    expires_at: datetime | None = None,
) -> list[Reservation]:
    """Hold ``lines`` (``[{"variant_id", "quantity"}, ...]``) for ``checkout``.

    Any previous active holds for the same checkout are returned to the pool
    first -- re-validating a checkout with a changed bag must end with exactly the
    holds the new snapshot needs, not a union of both. Because the whole thing is
    one transaction, an :class:`InsufficientStock` failure restores the old holds
    untouched.

    Raises:
        InsufficientStock: a line cannot be covered by available units.
    """
    from apps.shop.models import CheckoutSession  # runtime use; import is kept local

    checkout = CheckoutSession.objects.select_for_update().get(pk=checkout.pk)
    wanted = _normalise_lines(lines)

    previous = list(
        Reservation.objects.select_for_update()
        .filter(checkout=checkout, status=Reservation.Status.ACTIVE)
        .order_by("pk")
    )
    variant_ids = [line["variant_id"] for line in wanted] + [row.variant_id for row in previous]
    stock_rows = _lock_stock_rows(variant_ids)

    # Give back our own holds so a re-validation sees its own units as available.
    for row in previous:
        stock = stock_rows.get(row.variant_id)
        if stock is None:
            continue  # unreachable: a hold implies its stock row exists
        if not _transition(row, Reservation.Status.RELEASED):
            continue
        stock.reserved = max(stock.reserved - row.quantity, 0)
        stock.save(update_fields=["reserved", "updated_at"])
        _movement(stock.variant_id, InventoryMovement.Kind.RELEASED, 0, -row.quantity, reference)

    expires = expires_at or _hold_expiry()
    created: list[Reservation] = []
    for line in wanted:
        stock = stock_rows.get(line["variant_id"])
        if stock is None:
            stock = Stock.get_for_variant(_variant(line["variant_id"]))
            stock_rows[stock.variant_id] = stock
        if stock.available < line["quantity"]:
            raise InsufficientStock(stock.variant.sku, line["quantity"], stock.available)
        stock.reserved += line["quantity"]
        stock.save(update_fields=["reserved", "updated_at"])
        _movement(
            stock.variant_id,
            InventoryMovement.Kind.RESERVED,
            0,
            line["quantity"],
            reference,
        )
        created.append(
            Reservation.objects.create(
                variant_id=line["variant_id"],
                checkout=checkout,
                quantity=line["quantity"],
                expires_at=expires,
            )
        )
    return created


def _normalise_lines(lines: list[dict]) -> list[dict]:
    """Validate and de-duplicate snapshot lines (quantities merge per variant)."""
    merged: dict[int, int] = {}
    for line in lines:
        variant_id = int(line["variant_id"])
        quantity = int(line["quantity"])
        if quantity < 1:
            raise ValueError(f"quantity must be >= 1 for variant {variant_id}")
        merged[variant_id] = merged.get(variant_id, 0) + quantity
    return [{"variant_id": vid, "quantity": qty} for vid, qty in sorted(merged.items())]


def _variant(variant_id: int):
    from apps.catalog.models import ProductVariant

    return ProductVariant.objects.get(pk=variant_id)


def _transition(reservation: Reservation, new_status: str) -> bool:
    """Move an active hold to ``new_status``. Exactly one caller can win."""
    return (
        Reservation.objects.filter(pk=reservation.pk, status=Reservation.Status.ACTIVE).update(
            status=new_status
        )
        == 1
    )


def _movement(
    variant_id: int,
    kind: str,
    on_hand_delta: int,
    reserved_delta: int,
    reference: str,
    *,
    note: str = "",
    user=None,
) -> InventoryMovement:
    return InventoryMovement.objects.create(
        variant_id=variant_id,
        kind=kind,
        on_hand_delta=on_hand_delta,
        reserved_delta=reserved_delta,
        reference=reference,
        note=note,
        user=user,
    )


# =============================================================================
# Releasing and consuming
# =============================================================================


@transaction.atomic
def release_holds(
    holds,
    *,
    kind: str = InventoryMovement.Kind.RELEASED,
    reference: str = "",
    note: str = "",
) -> int:
    """Return active holds to the pool. Idempotent: replayed calls are no-ops.

    Accepts a queryset or an iterable of :class:`Reservation`. Returns the number
    of holds that actually transitioned.
    """
    return _settle(
        holds,
        new_status=Reservation.Status.RELEASED,
        movement_kind=kind,
        consume_on_hand=False,
        reference=reference,
        note=note,
    )


@transaction.atomic
def consume_holds(holds, *, reference: str = "", note: str = "") -> int:
    """Turn active holds into sold units (``on_hand`` and ``reserved`` both drop).

    Idempotent for the same reason as :func:`release_holds`. Returns the number of
    holds that actually transitioned.

    Raises:
        InsufficientStock: counters disagree with the hold (corruption guard; the
            invariant ``reserved <= on_hand`` makes this unreachable normally).
    """
    return _settle(
        holds,
        new_status=Reservation.Status.CONSUMED,
        movement_kind=InventoryMovement.Kind.SOLD,
        consume_on_hand=True,
        reference=reference,
        note=note,
    )


def _settle(
    holds,
    *,
    new_status: str,
    movement_kind: str,
    consume_on_hand: bool,
    reference: str,
    note: str,
) -> int:
    rows = list(holds)
    if not rows:
        return 0

    stock_rows = _lock_stock_rows([row.variant_id for row in rows])
    settled = 0
    for row in rows:
        if not _transition(row, new_status):
            continue  # already settled -- the replay this call is meant to absorb
        stock = stock_rows.get(row.variant_id)
        if stock is None:
            stock = Stock.get_for_variant(_variant(row.variant_id))
            stock_rows[stock.variant_id] = stock
        if consume_on_hand:
            if stock.on_hand < row.quantity or stock.reserved < row.quantity:
                raise InsufficientStock(stock.variant.sku, row.quantity, stock.available)
            stock.on_hand -= row.quantity
            stock.reserved -= row.quantity
            stock.save(update_fields=["on_hand", "reserved", "updated_at"])
            _movement(
                stock.variant_id,
                movement_kind,
                -row.quantity,
                -row.quantity,
                reference,
                note=note,
            )
        else:
            if stock.reserved < row.quantity:
                raise InsufficientStock(stock.variant.sku, row.quantity, stock.available)
            stock.reserved -= row.quantity
            stock.save(update_fields=["reserved", "updated_at"])
            _movement(stock.variant_id, movement_kind, 0, -row.quantity, reference, note=note)
        settled += 1
    return settled


# =============================================================================
# Order hand-off and expiry
# =============================================================================


def attach_holds_to_order(checkout: CheckoutSession, order: Order) -> int:
    """Point the checkout's active holds at ``order`` so the sweeper leaves them.

    Called inside the order-creation transaction, after the checkout row lock has
    been taken, so a concurrent sweep sees either no holds or already-attached
    ones. Returns the number of holds attached.
    """
    return Reservation.objects.filter(
        checkout=checkout, status=Reservation.Status.ACTIVE, order__isnull=True
    ).update(order=order)


def active_holds_for_order(order: Order):
    """The order's still-active holds (a queryset, so callers can chain locks)."""
    return Reservation.objects.filter(order=order, status=Reservation.Status.ACTIVE).order_by("pk")


@transaction.atomic
def sweep_expired_holds(*, now: datetime | None = None) -> int:
    """Release expired holds that no placed order owns.

    An abandoned checkout gives its units back; a hold attached to an order lives
    until that order is paid or cancelled, because an unpaid order that quietly
    lost its stock would be worse than one that keeps it.
    """
    moment = now or timezone.now()
    expired = list(
        Reservation.objects.select_for_update()
        .filter(
            status=Reservation.Status.ACTIVE,
            expires_at__lte=moment,
            order__isnull=True,
        )
        .order_by("pk")
    )
    return _settle(
        expired,
        new_status=Reservation.Status.EXPIRED,
        movement_kind=InventoryMovement.Kind.EXPIRED,
        consume_on_hand=False,
        reference="sweeper",
        note="Reservation expired without an order.",
    )


# =============================================================================
# Manual stock (admin workflow)
# =============================================================================


@transaction.atomic
def adjust_stock(
    variant,
    delta: int,
    *,
    kind: str = InventoryMovement.Kind.ADJUSTMENT,
    user=None,
    note: str = "",
    reference: str = "manual",
) -> Stock:
    """Apply ``delta`` units to ``variant``'s on-hand counter, ledger first.

    Raises:
        InsufficientStock: the adjustment would push on-hand below the units that
            are currently held (or below zero).
    """
    if not delta:
        raise ValueError("delta must be non-zero")

    stock = Stock.get_for_variant(variant)
    stock = Stock.objects.select_for_update().get(pk=stock.pk)
    new_on_hand = stock.on_hand + delta
    if new_on_hand < 0:
        raise InsufficientStock(stock.variant.sku, -delta, stock.on_hand, idempotent=True)
    if new_on_hand < stock.reserved:
        raise InsufficientStock(
            stock.variant.sku,
            stock.reserved - new_on_hand,
            stock.available,
            idempotent=True,
        )
    stock.on_hand = new_on_hand
    stock.save(update_fields=["on_hand", "updated_at"])
    _movement(
        stock.variant_id,
        kind,
        delta,
        0,
        reference,
        note=note,
        user=user,
    )
    return stock
