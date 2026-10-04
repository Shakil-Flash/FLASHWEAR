"""FLASH Points services: balances, earning, redemption holds and expiry (Phase 7).

The **ledger is the balance**. There is no cached column to drift: available points are
``SUM(ledger) - SUM(active reservations)``, and every decision path reads that pair under the
same lock (the customer's ``auth_user`` row) so two checkouts cannot spend the same points.
Locking only the user row keeps loyalty's lock order trivial: user, then nothing else it owns.

Rules the services enforce, in one place:

* **Earning** happens only when an order is paid (via the ``order_paid`` signal), on
  ``subtotal - discount_amount`` -- shipping is not merchandise and redeemed points do not earn
  points. Awarding is idempotent on ``order:<number>``.
* **Redemption** is a two-phase hold: :func:`reserve_for_checkout` pins points to a checkout,
  the handoff attaches the hold to the order, payment success converts it to a negative
  ``REDEMPTION`` ledger row, cancellation/expiry releases it. A hold attached to a placed order
  is never swept -- exactly like stock holds.
* **Money math** is integer points against ``LOYALTY_REDEEM_RATE`` (points per currency unit),
  validated in increments of ``LOYALTY_REDEEM_INCREMENT`` and capped by both the customer's
  balance and ``LOYALTY_MAX_REDEEM_PERCENT`` of the eligible subtotal.
* **Expiry** is deferred while a customer has any active hold, so a held earn cannot be
  double-spent by the sweeper racing a checkout; each expiry row is idempotent on
  ``expiry:<ledger pk>``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.catalog.templatetags.catalog_extras import money
from apps.engagement.models import PointsReservation, PointsTransaction
from apps.engagement.services.errors import LoyaltyError

TWO_PLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


def balance_for(user, *, excluding_checkout=None) -> int:
    """Spendable points: ledger total minus what checkouts are currently holding.

    ``excluding_checkout`` lets a checkout that is *re-applying* its own hold see its previous
    hold as free money -- validation must not destroy the current hold before the replacement
    is proven.
    """
    ledger = (
        PointsTransaction.objects.filter(user=user).aggregate(total=Sum("amount"))["total"] or 0
    )
    held_qs = PointsReservation.objects.filter(user=user, status=PointsReservation.Status.ACTIVE)
    if excluding_checkout is not None:
        held_qs = held_qs.exclude(checkout=excluding_checkout)
    held = held_qs.aggregate(total=Sum("points"))["total"] or 0
    return int(ledger - held)


def held_for(user) -> int:
    """Points currently pinned to this customer's checkouts (display only)."""
    held = (
        PointsReservation.objects.filter(
            user=user, status=PointsReservation.Status.ACTIVE
        ).aggregate(total=Sum("points"))["total"]
        or 0
    )
    return int(held)


# ---------------------------------------------------------------------------
# Earning (driven by the order_paid signal)
# ---------------------------------------------------------------------------


def earn_points_for_order(order) -> PointsTransaction | None:
    """Award ``subtotal - discount`` worth of points for a paid order. Idempotent."""
    base = order.subtotal - order.discount_amount
    if base < 0:
        base = Decimal("0")
    points = int(base * Decimal(settings.LOYALTY_EARN_RATE))
    if points <= 0:
        return None

    entry, _ = PointsTransaction.objects.get_or_create(
        reference=f"order:{order.number}",
        transaction_type=PointsTransaction.TransactionType.PURCHASE_EARN,
        defaults={
            "user_id": order.user_id,
            "amount": points,
            "order": order,
            "expires_at": timezone.now() + timedelta(days=int(settings.LOYALTY_EXPIRY_DAYS)),
            "note": "Earned on purchase.",
        },
    )
    return entry


def reverse_earn_for_order(order, *, note: str = "") -> PointsTransaction | None:
    """Offset a previous earn (refund path). Idempotent on ``refund:<earn pk>``.

    No Phase 6/7 flow reaches ``REFUNDED`` yet; this exists so the refund service has a tested
    one-liner when the returns phase lands, and it is exercised directly by the test suite.
    """
    earn = PointsTransaction.objects.filter(
        order=order,
        transaction_type=PointsTransaction.TransactionType.PURCHASE_EARN,
    ).first()
    if earn is None:
        return None

    entry, _ = PointsTransaction.objects.get_or_create(
        reference=f"refund:{earn.pk}",
        transaction_type=PointsTransaction.TransactionType.REFUND_REVERSAL,
        defaults={
            "user_id": order.user_id,
            "amount": -earn.amount,
            "order": order,
            "note": note or "Earn reversed after refund.",
        },
    )
    return entry


# ---------------------------------------------------------------------------
# Redemption: reserve -> attach -> consume / release
# ---------------------------------------------------------------------------


def redeem_discount(points: int) -> Decimal:
    """Points -> currency, rounded to the penny (100 points at rate 100 is 1.00)."""
    return (Decimal(int(points)) / Decimal(settings.LOYALTY_REDEEM_RATE)).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )


def max_redeemable_points(user, *, eligible_subtotal: Decimal, excluding_checkout=None) -> int:
    """Largest increment-aligned point count redeemable on ``eligible_subtotal``.

    Three caps, all floors: the customer's spendable balance (optionally ignoring one
    checkout's own hold), ``LOYALTY_MAX_REDEEM_PERCENT`` of the eligible subtotal, and the
    increment grid. Returns 0 when the configured minimum order amount is not reached or the
    balance is empty.
    """
    if eligible_subtotal < Decimal(settings.LOYALTY_MIN_ORDER_AMOUNT):
        return 0

    increment = int(settings.LOYALTY_REDEEM_INCREMENT)
    balance_cap = max(0, balance_for(user, excluding_checkout=excluding_checkout))
    balance_points = (balance_cap // increment) * increment

    percent_cap = eligible_subtotal * Decimal(settings.LOYALTY_MAX_REDEEM_PERCENT) / Decimal("100")
    percent_points = int(percent_cap * Decimal(settings.LOYALTY_REDEEM_RATE))
    percent_points = (percent_points // increment) * increment

    return max(min(balance_points, percent_points), 0)


def validate_redemption(
    user, points, *, eligible_subtotal: Decimal, excluding_checkout=None
) -> int:
    """Turn customer input into a safe point count, or raise :class:`LoyaltyError`."""
    try:
        points = int(points)
    except (TypeError, ValueError) as exc:
        raise LoyaltyError("Enter a whole number of points.", code="invalid") from exc

    if points <= 0:
        raise LoyaltyError("Enter how many FLASH Points to redeem.", code="invalid")

    increment = int(settings.LOYALTY_REDEEM_INCREMENT)
    if points % increment:
        raise LoyaltyError(f"FLASH Points redeem in multiples of {increment}.", code="increment")

    maximum = max_redeemable_points(
        user, eligible_subtotal=eligible_subtotal, excluding_checkout=excluding_checkout
    )
    if points > maximum:
        spendable = max(0, balance_for(user, excluding_checkout=excluding_checkout))
        if points > spendable:
            raise LoyaltyError("You do not have that many FLASH Points.", code="insufficient")
        raise LoyaltyError(
            f"You can redeem at most {maximum} points on this order.", code="over_limit"
        )
    return points


def reserve_for_checkout(checkout, points) -> PointsReservation | None:
    """Pin ``points`` to ``checkout``, replacing any hold it already had.

    Validates against the balance **excluding the checkout's own hold**, so re-applying with a
    smaller number works and re-applying with an impossible number fails without destroying the
    hold that is already in place. ``points == 0`` releases the hold and returns ``None``.
    """
    points = int(points or 0)
    with transaction.atomic():
        # One customer, one active hold decision at a time: serialise on the user row so two
        # concurrent checkouts cannot both read the same free balance.
        get_user_model().objects.select_for_update().filter(pk=checkout.user_id).first()

        if points <= 0:
            return _release_checkout_hold(checkout)

        available = balance_for(checkout.user, excluding_checkout=checkout)
        if points > available:
            raise LoyaltyError("You do not have that many FLASH Points.", code="insufficient")

        _release_checkout_hold(checkout)
        return PointsReservation.objects.create(
            user=checkout.user,
            checkout=checkout,
            points=points,
            expires_at=timezone.now()
            + timedelta(minutes=int(settings.LOYALTY_RESERVATION_MINUTES)),
        )


def attach_points_to_order(checkout, order) -> PointsReservation | None:
    """Move the checkout's active hold onto the placed order (status stays active)."""
    hold = PointsReservation.objects.filter(
        checkout=checkout, status=PointsReservation.Status.ACTIVE
    ).first()
    if hold is None:
        return None
    PointsReservation.objects.filter(pk=hold.pk, status=PointsReservation.Status.ACTIVE).update(
        order=order
    )
    hold.order = order
    return hold


def consume_points_for_order(order) -> PointsTransaction | None:
    """On payment success: hold -> consumed, plus the negative ledger row. Idempotent."""
    with transaction.atomic():
        hold = PointsReservation.objects.filter(
            order=order, status=PointsReservation.Status.ACTIVE
        ).first()
        if hold is None:
            return None
        transitioned = PointsReservation.objects.filter(
            pk=hold.pk, status=PointsReservation.Status.ACTIVE
        ).update(status=PointsReservation.Status.CONSUMED)
        if not transitioned:
            return None
        entry, _ = PointsTransaction.objects.get_or_create(
            reference=f"order:{order.number}",
            transaction_type=PointsTransaction.TransactionType.REDEMPTION,
            defaults={
                "user_id": order.user_id,
                "amount": -hold.points,
                "order": order,
                "note": "Redeemed at checkout.",
            },
        )
        return entry


def release_points_for_order(order) -> int:
    """On cancellation/failure: hand held points back. Returns holds released."""
    return PointsReservation.objects.filter(
        order=order, status=PointsReservation.Status.ACTIVE
    ).update(status=PointsReservation.Status.RELEASED)


def _release_checkout_hold(checkout) -> None:
    """Conditional release of the checkout's own hold (history is preserved as RELEASED)."""
    PointsReservation.objects.filter(
        checkout=checkout, status=PointsReservation.Status.ACTIVE
    ).update(status=PointsReservation.Status.RELEASED)


# ---------------------------------------------------------------------------
# Sweepers (celery beat)
# ---------------------------------------------------------------------------


def sweep_expired_reservations(*, now=None) -> int:
    """Expire checkout-only holds past their timer. Order-attached holds are never touched."""
    moment = now or timezone.now()
    return PointsReservation.objects.filter(
        status=PointsReservation.Status.ACTIVE,
        order__isnull=True,
        expires_at__lte=moment,
    ).update(status=PointsReservation.Status.EXPIRED)


def expire_due_points(*, now=None) -> int:
    """Write offsetting EXPIRATION rows for earns past their expiry date.

    Idempotent per earn (``expiry:<pk>`` is unique with the type). Deferred entirely for any
    customer holding an active reservation: expiring an earn that is currently reserved would
    subtract the same points twice.
    """
    moment = now or timezone.now()
    due = list(
        PointsTransaction.objects.filter(
            expires_at__isnull=False,
            expires_at__lte=moment,
        )
        .exclude(transaction_type=PointsTransaction.TransactionType.EXPIRATION)
        .order_by("pk")
    )
    if not due:
        return 0

    already = set(
        PointsTransaction.objects.filter(
            transaction_type=PointsTransaction.TransactionType.EXPIRATION,
            reference__in=[f"expiry:{entry.pk}" for entry in due],
        ).values_list("reference", flat=True)
    )
    pending = [entry for entry in due if f"expiry:{entry.pk}" not in already]
    if not pending:
        return 0

    expired = 0
    by_user: dict[int, list] = {}
    for entry in pending:
        by_user.setdefault(entry.user_id, []).append(entry)

    for user_id, entries in by_user.items():
        with transaction.atomic():
            get_user_model().objects.select_for_update().filter(pk=user_id).first()
            if PointsReservation.objects.filter(
                user_id=user_id, status=PointsReservation.Status.ACTIVE
            ).exists():
                continue
            for entry in entries:
                try:
                    with transaction.atomic():
                        PointsTransaction.objects.create(
                            user_id=entry.user_id,
                            transaction_type=PointsTransaction.TransactionType.EXPIRATION,
                            reference=f"expiry:{entry.pk}",
                            amount=-entry.amount,
                            note=f"Points from {entry.reference} expired.",
                        )
                        expired += 1
                except IntegrityError:
                    # A concurrent sweeper won the same row; the unique constraint is the
                    # idempotency guarantee, so losing is correct.
                    continue
    return expired


# ---------------------------------------------------------------------------
# Operator tools
# ---------------------------------------------------------------------------


def adjust_balance(user, amount: int, *, note: str = "") -> PointsTransaction:
    """Manual credit/debit from the admin. Any sign except zero, one row, unique reference."""
    amount = int(amount)
    if amount == 0:
        raise LoyaltyError("Adjustment must be a non-zero number of points.", code="invalid")
    return PointsTransaction.objects.create(
        user=user,
        transaction_type=PointsTransaction.TransactionType.ADMIN_ADJUSTMENT,
        reference=f"adjust:{uuid.uuid4().hex}",
        amount=amount,
        note=note,
    )


def upcoming_expirations(user, *, limit: int = 5):
    """Future expiring earns for the dashboard, soonest first. Future rows cannot be expired yet."""
    return PointsTransaction.objects.filter(
        user=user,
        expires_at__isnull=False,
        expires_at__gt=timezone.now(),
    ).order_by("expires_at")[:limit]


def redemption_message(user) -> str:
    """Copy for the checkout panel: the rate, straight from settings, in store currency."""
    rate = int(settings.LOYALTY_REDEEM_RATE)
    return f"{rate} points = {money(Decimal(1))} off your order."
