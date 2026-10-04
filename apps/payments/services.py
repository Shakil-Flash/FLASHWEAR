"""Payment orchestration: start attempts and apply provider events exactly once.

The pipeline a payment travels::

    start_payment()            create + register        (outside any transaction)
    handle_provider_event()    authenticate -> record -> transition -> side effects
    cancel_payment()           customer gave up         (provider call first, then DB)

Side effects on success and failure are decided here, in one place:

* **Succeeded** -- stock holds are consumed (units actually leave the warehouse
  counters), the order moves to ``PAID``, and ``order_paid`` fires so FLASH
  Points are awarded/redemptions consumed in the same transaction.
* **Failed / cancelled** -- the order is cancelled through
  :func:`apps.orders.services.cancel_order`, which releases the holds and fires
  ``order_cancelled`` (releasing any held points).

Idempotency has two independent layers: the ``(provider, event_id)`` unique
constraint on :class:`~apps.payments.models.PaymentEvent` (so a redelivered
webhook writes nothing) and every state transition being conditional on the
current status (so even a second *different* event cannot double-apply).

Lock order: payment -> order -> reservation -> stock rows (ascending variant id).
"""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from apps.inventory.services import consume_holds
from apps.orders.models import Order, OrderEvent
from apps.orders.services import cancel_order
from apps.orders.signals import order_paid
from apps.payments.models import Payment, PaymentEvent
from apps.payments.providers import get_provider, provider_name
from apps.payments.providers.base import ProviderEvent, ProviderIntent, WebhookError

logger = logging.getLogger(__name__)

__all__ = ["cancel_payment", "handle_provider_event", "start_payment"]


# =============================================================================
# Starting an attempt
# =============================================================================


def start_payment(order: Order) -> Payment:
    """Create (or reuse) the order's payment and register it with the provider.

    The provider call deliberately sits between two small transactions: no
    external API call may run inside one. The development provider is local, but
    this is the exact shape a real integration must follow -- and retrying is
    safe because provider references are deterministic per payment.
    """
    payment = _ensure_payment(order)
    if payment.status == Payment.Status.CREATED:
        intent = get_provider().create_payment(payment)  # no open transaction here
        payment = _record_intent(payment, intent)
    return payment


def _ensure_payment(order: Order) -> Payment:
    """The order's payment row, created on first use (one per order, by schema)."""
    with transaction.atomic():
        existing = Payment.objects.filter(order=order).first()
        if existing is not None:
            return existing
        return Payment.objects.create(
            order=order,
            provider=provider_name(),
            amount=order.total,
            currency=order.currency,
        )


def _record_intent(payment: Payment, intent: ProviderIntent) -> Payment:
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if not payment.provider_reference:
            payment.provider_reference = intent.reference
            payment.save(update_fields=["provider_reference", "updated_at"])
        if payment.status == Payment.Status.CREATED and intent.status != payment.status:
            payment.transition_to(intent.status)
        if payment.status == Payment.Status.PENDING:
            _pending_event(
                payment.order,
                "Payment initiated.",
                metadata={
                    "payment": payment.pk,
                    "reference": payment.provider_reference,
                },
            )
        return payment


# =============================================================================
# Cancelling (customer gave up)
# =============================================================================


def cancel_payment(payment: Payment, *, actor=None, reason: str = "") -> Payment:
    """Cancel an attempt that has not succeeded, and with it its order.

    The provider is asked first (outside the transaction); the rest -- payment
    state, order cancellation, stock release -- is one atomic block.

    A payment that already reached a terminal state is returned untouched: this
    function is safe to call twice (double-clicked button, retried webhook).
    """
    if payment.status in (
        Payment.Status.SUCCEEDED,
        Payment.Status.FAILED,
        Payment.Status.CANCELLED,
    ):
        return payment

    intent = get_provider().cancel_payment(payment)  # no open transaction here

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.is_settled:
            return payment
        if payment.status == Payment.Status.CREATED:
            payment.provider_reference = payment.provider_reference or intent.reference
            payment.save(update_fields=["provider_reference", "updated_at"])
        payment.transition_to(intent.status)
        PaymentEvent.objects.create(
            payment=payment,
            provider=payment.provider,
            event_id=f"cancel-{payment.pk}-{payment.updated_at.timestamp()}",
            event_type=Payment.Status.CANCELLED,
            payload={"reason": reason or "cancelled by customer"},
        )
        order = payment.order
        if order.status == Order.Status.PENDING_PAYMENT:
            cancel_order(
                order,
                actor=actor,
                note=reason or "Payment cancelled.",
                event_type=OrderEvent.Type.PAYMENT_CANCELLED,
            )
        return payment


# =============================================================================
# Applying a provider event (webhook / simulation)
# =============================================================================


def handle_provider_event(provider: str, event: ProviderEvent) -> PaymentEvent:
    """Apply one authenticated provider event. Returns the stored event row.

    The event row is inserted first, inside the same transaction as the state
    change: if the ``(provider, event_id)`` pair already exists the insert fails
    on the unique constraint, we return the stored row and change nothing. That
    is the whole idempotency story -- no caches, no "have we seen this?" query
    that a race could slip past.

    Raises:
        WebhookError: unknown payment, mismatched amount, or an impossible
            transition. The caller (webhook view) answers 4xx so the provider
            stops retrying something that will never apply.
    """
    if provider != provider_name():
        raise WebhookError(f"Events for {provider!r} are not accepted here.")

    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update()
            .filter(provider=provider, provider_reference=event.payment_reference)
            .first()
        )
        if payment is None:
            raise WebhookError(f"No payment with reference {event.payment_reference!r}.")
        if event.amount != payment.amount or event.currency != payment.currency:
            raise WebhookError("Event amount/currency does not match the payment.")

        record = _store_event(payment, provider, event)
        if record is None:
            # Duplicate delivery: the first one already did the work.
            return PaymentEvent.objects.get(provider=provider, event_id=event.event_id)

        _apply(payment, event, record)
        return record


def _store_event(payment: Payment, provider: str, event: ProviderEvent) -> PaymentEvent | None:
    """Insert the event; ``None`` means this delivery is a duplicate."""
    try:
        with transaction.atomic():  # savepoint: IntegrityError must not poison the caller
            return PaymentEvent.objects.create(
                payment=payment,
                provider=provider,
                event_id=event.event_id,
                event_type=event.event_type,
                payload=event.as_payload(),
            )
    except IntegrityError:
        return None


def _apply(payment: Payment, event: ProviderEvent, record: PaymentEvent) -> None:
    """Turn the recorded event into payment, order and stock movement."""
    target = event.event_type

    if payment.status == target:
        return  # same state, different event id -- recorded, nothing else to do
    if not payment.can_transition_to(target):
        raise WebhookError(f"Payment cannot go from {payment.status} to {target}.")

    payment.transition_to(target)

    if target == Payment.Status.SUCCEEDED:
        _on_succeeded(payment, event)
    elif target == Payment.Status.FAILED:
        payment.failure_message = (event.reason or "Payment failed.")[:200]
        payment.failure_code = "provider_failed"
        payment.save(update_fields=["failure_code", "failure_message", "updated_at"])
        _on_unpaid(payment, event, OrderEvent.Type.PAYMENT_FAILED, "Payment failed.")
    elif target == Payment.Status.CANCELLED:
        _on_unpaid(payment, event, OrderEvent.Type.PAYMENT_CANCELLED, "Payment cancelled.")
    elif target == Payment.Status.PENDING:
        _pending_event(
            payment.order, event.reason or "Payment is pending.", metadata={"event": event.event_id}
        )


def _pending_event(order: Order, note: str, *, metadata: dict) -> None:
    """Record "payment is pending" once per order (intent and webhook can both say it)."""
    if order.status != Order.Status.PENDING_PAYMENT:
        return
    OrderEvent.objects.get_or_create(
        order=order,
        event_type=OrderEvent.Type.PAYMENT_PENDING,
        defaults={"note": note[:300], "metadata": metadata},
    )


def _lock_order(payment: Payment) -> Order:
    return Order.objects.select_for_update().get(pk=payment.order_id)


def _on_succeeded(payment: Payment, event: ProviderEvent) -> None:
    order = _lock_order(payment)
    consumed = consume_holds(
        order.reservations.all(),
        reference=f"order:{order.number}",
        note=f"Payment {event.event_id}",
    )
    if order.status == Order.Status.PENDING_PAYMENT:
        order.transition_to(
            Order.Status.PAID,
            event_type=OrderEvent.Type.PAYMENT_SUCCEEDED,
            note="Payment received.",
            metadata={
                "payment": payment.pk,
                "event": event.event_id,
                "amount": str(payment.amount),
            },
        )
        OrderEvent.objects.create(
            order=order,
            event_type=OrderEvent.Type.STOCK_CONSUMED,
            note="Held stock sold.",
            metadata={"units": consumed},
        )
        # FLASH Points earning/redemption runs in this same transaction (see
        # apps.orders.signals): points must be transactional with the payment.
        order_paid.send(sender=Order, order=order)
    elif order.status == Order.Status.CANCELLED:
        # Money arrived for an order that was already given up: never silent.
        logger.error("Payment %s succeeded for cancelled order %s", payment.pk, order.number)
        OrderEvent.objects.create(
            order=order,
            event_type=OrderEvent.Type.NOTE,
            note="Payment succeeded after cancellation -- manual refund required.",
            metadata={"payment": payment.pk, "event": event.event_id},
        )


def _on_unpaid(payment: Payment, event: ProviderEvent, event_type: str, note: str) -> None:
    order = _lock_order(payment)
    if order.status == Order.Status.PENDING_PAYMENT:
        cancel_order(
            order,
            note=f"{note} {event.reason}".strip(),
            event_type=event_type,
        )
    else:
        OrderEvent.objects.create(
            order=order,
            event_type=event_type,
            note=note,
            metadata={"event": event.event_id, "order_status": order.status},
        )
