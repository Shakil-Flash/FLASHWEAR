"""Returns, Exchanges and Refund domain services (Phase 23).

Authoritative workflows for:
1. Return eligibility calculation (server-side, non-forgeable).
2. Customer return & exchange request creation.
3. Customer cancellation of pending requests.
4. Staff approval, rejection, parcel receipt, inspection.
5. Refund execution, authoritative inventory restock and loyalty adjustment.
6. Exchange stock allocation and price difference resolution.
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryMovement, Stock
from apps.inventory.services import InsufficientStock, adjust_stock
from apps.notifications.models import NotificationType
from apps.notifications.services.events import emit as emit_notification
from apps.orders.models import (
    InvalidTransition,
    Order,
    OrderEvent,
    OrderItem,
    ReturnEvent,
    ReturnItem,
    ReturnRequest,
)
from apps.payments.models import Payment, Refund
from apps.payments.services import process_refund

logger = logging.getLogger(__name__)

__all__ = [
    "approve_return_request",
    "cancel_return_request",
    "check_order_return_eligibility",
    "create_return_request",
    "generate_return_number",
    "inspect_return",
    "mark_return_received",
    "process_exchange_for_return",
    "process_refund_for_return",
    "reject_return_request",
]


def generate_return_number() -> str:
    """Generate public handle: RET-<yyyymmdd>-<8 random chars>."""
    stamp = timezone.now().strftime("%Y%m%d")
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"RET-{stamp}-{suffix}"


# =============================================================================
# 1. Eligibility Check
# =============================================================================


def check_order_return_eligibility(order: Order) -> dict[str, Any]:
    """Calculate whether and to what extent *order* is eligible for a return/exchange.

    Rules enforced:
    1. Order must have been paid and delivered (or shipped).
    2. Payment must have succeeded.
    3. Delivery must be within the configured return window (RETURN_WINDOW_DAYS).
    4. Units previously requested for return (and not rejected/cancelled) cannot be re-returned.
    5. At least one line item must have remaining returnable quantity > 0.
    """
    window_days = getattr(settings, "RETURN_WINDOW_DAYS", 30)

    # Status check
    eligible_statuses = (Order.Status.DELIVERED, Order.Status.SHIPPED)
    if order.status not in eligible_statuses:
        return {
            "is_eligible": False,
            "reason": _(
                "Orders can only be returned once shipped or delivered. Current status: %(status)s."
            )
            % {"status": order.get_status_display()},
            "window_days": window_days,
            "delivered_at": order.delivered_at,
            "deadline": None,
            "days_remaining": 0,
            "items": [],
        }

    # Payment check
    payment = getattr(order, "payment", None)
    if not payment or payment.status not in (Payment.Status.SUCCEEDED, Payment.Status.REFUNDED):
        return {
            "is_eligible": False,
            "reason": _("Only successfully paid orders can be returned."),
            "window_days": window_days,
            "delivered_at": order.delivered_at,
            "deadline": None,
            "days_remaining": 0,
            "items": [],
        }

    # Return window calculation
    reference_time = order.delivered_at or order.shipped_at or order.created_at
    deadline = reference_time + timedelta(days=window_days)
    now = timezone.now()

    if now > deadline:
        return {
            "is_eligible": False,
            "reason": _("The %(window)s-day return window for this order closed on %(date)s.")
            % {"window": window_days, "date": deadline.strftime("%Y-%m-%d")},
            "window_days": window_days,
            "delivered_at": order.delivered_at,
            "deadline": deadline,
            "days_remaining": 0,
            "items": [],
        }

    days_remaining = max(0, (deadline - now).days)

    # Active return items for this order: exclude rejected and cancelled
    active_returns = order.returns.exclude(
        status__in=(ReturnRequest.Status.CANCELLED, ReturnRequest.Status.REJECTED)
    )
    active_returned_by_item: dict[int, int] = {}
    for ret in active_returns:
        for r_item in ret.items.all():
            active_returned_by_item[r_item.order_item_id] = (
                active_returned_by_item.get(r_item.order_item_id, 0) + r_item.quantity
            )

    items_report = []
    any_returnable = False

    for line in order.items.all():
        already_returned = active_returned_by_item.get(line.pk, 0)
        remaining = max(0, line.quantity - already_returned)
        is_item_eligible = remaining > 0
        if is_item_eligible:
            any_returnable = True

        items_report.append(
            {
                "order_item_id": line.pk,
                "sku": line.sku,
                "product_name": line.product_name,
                "option_label": line.option_label,
                "variant_id": line.variant_id,
                "unit_price": str(line.unit_price),
                "line_total": str(line.line_total),
                "purchased_quantity": line.quantity,
                "already_returned_quantity": already_returned,
                "remaining_quantity": remaining,
                "is_eligible": is_item_eligible,
            }
        )

    if not any_returnable:
        return {
            "is_eligible": False,
            "reason": _(
                "All eligible items on this order have already been returned or requested."
            ),
            "window_days": window_days,
            "delivered_at": order.delivered_at,
            "deadline": deadline,
            "days_remaining": days_remaining,
            "items": items_report,
        }

    return {
        "is_eligible": True,
        "reason": "",
        "window_days": window_days,
        "delivered_at": order.delivered_at,
        "deadline": deadline,
        "days_remaining": days_remaining,
        "items": items_report,
    }


# =============================================================================
# 2. Creating a Return Request
# =============================================================================


@transaction.atomic
def create_return_request(
    order: Order,
    user,
    items_data: list[dict[str, Any]],
    *,
    return_type: str = ReturnRequest.ReturnType.REFUND,
    reason: str = ReturnRequest.Reason.SIZE_FIT,
    customer_note: str = "",
) -> ReturnRequest:
    """Submit a return or exchange request.

    *items_data* is a list of dicts:
    [
        {
            "order_item_id": int,
            "quantity": int,
            "reason": str (optional),
            "customer_note": str (optional),
            "replacement_variant_id": int (required if return_type == EXCHANGE),
        },
        ...
    ]
    """
    if order.user_id != user.id:
        raise ValidationError(_("You cannot request a return for another user's order."))

    if not items_data:
        raise ValidationError(_("Please select at least one item to return."))

    if return_type not in ReturnRequest.ReturnType.values:
        raise ValidationError(_("Invalid return type: %(type)s.") % {"type": return_type})

    if reason not in ReturnRequest.Reason.values:
        raise ValidationError(_("Invalid return reason: %(reason)s.") % {"reason": reason})

    order = Order.objects.select_for_update().get(pk=order.pk)
    eligibility = check_order_return_eligibility(order)
    if not eligibility["is_eligible"]:
        raise ValidationError(eligibility["reason"] or _("This order is not eligible for return."))

    eligible_items_map = {item["order_item_id"]: item for item in eligibility["items"]}

    return_number = generate_return_number()
    return_request = ReturnRequest.objects.create(
        number=return_number,
        order=order,
        user=user,
        status=ReturnRequest.Status.REQUESTED,
        return_type=return_type,
        reason=reason,
        customer_note=customer_note.strip(),
    )

    for item_data in items_data:
        order_item_id = item_data.get("order_item_id")
        try:
            order_item = order.items.get(pk=order_item_id)
        except OrderItem.DoesNotExist as exc:
            raise ValidationError(
                _("Order item %(id)s does not belong to this order.") % {"id": order_item_id}
            ) from exc

        qty = int(item_data.get("quantity") or 0)
        if qty <= 0:
            raise ValidationError(_("Return quantity must be at least 1."))

        elig_info = eligible_items_map.get(order_item_id)
        if not elig_info or qty > elig_info["remaining_quantity"]:
            remaining = elig_info["remaining_quantity"] if elig_info else 0
            raise ValidationError(
                _("Cannot return %(qty)s of %(name)s. Only %(remaining)s remaining.")
                % {"qty": qty, "name": order_item.product_name, "remaining": remaining}
            )

        replacement_variant = None
        price_diff = Decimal("0.00")

        if return_type == ReturnRequest.ReturnType.EXCHANGE:
            replacement_id = item_data.get("replacement_variant_id")
            if not replacement_id:
                raise ValidationError(
                    _("A replacement variant must be specified for exchange of %(name)s.")
                    % {"name": order_item.product_name}
                )
            try:
                replacement_variant = ProductVariant.objects.select_related("product").get(
                    pk=replacement_id, is_active=True
                )
            except ProductVariant.DoesNotExist as exc:
                raise ValidationError(
                    _("Selected replacement variant does not exist or is inactive.")
                ) from exc

            # Ensure the replacement has available stock
            rep_stock = Stock.get_for_variant(replacement_variant)
            if rep_stock.available < qty:
                raise InsufficientStock(replacement_variant.sku, qty, rep_stock.available)

            price_diff = (replacement_variant.price - order_item.unit_price) * Decimal(qty)

        item_reason = item_data.get("reason") or reason
        if item_reason not in ReturnRequest.Reason.values:
            item_reason = reason

        ReturnItem.objects.create(
            return_request=return_request,
            order_item=order_item,
            quantity=qty,
            reason=item_reason,
            customer_note=str(item_data.get("customer_note") or "").strip(),
            replacement_variant=replacement_variant,
            price_difference=price_diff,
        )

    ReturnEvent.objects.create(
        return_request=return_request,
        event_type="created",
        actor=user,
        note=_("Customer requested %(type)s for %(count)s line items.")
        % {"type": return_type, "count": len(items_data)},
        metadata={"return_type": return_type, "reason": reason},
    )

    OrderEvent.objects.create(
        order=order,
        event_type=OrderEvent.Type.STATUS_CHANGED,
        actor=user,
        note=_("Return request %(number)s submitted by customer.") % {"number": return_number},
        metadata={"return_request": return_number},
    )

    # Notify customer
    _notify_return(
        return_request,
        NotificationType.RETURN_REQUESTED,
        "created",
        context={"order_number": order.number, "return_number": return_number},
    )

    return return_request


# =============================================================================
# 3. Staff Approval & Rejection
# =============================================================================


@transaction.atomic
def approve_return_request(
    return_request: ReturnRequest,
    *,
    staff_user=None,
    note: str = "",
) -> ReturnRequest:
    """Approve a customer return request."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    return_request.transition_to(
        ReturnRequest.Status.APPROVED,
        actor=staff_user,
        note=note or _("Return approved by staff."),
    )

    _notify_return(
        return_request,
        NotificationType.RETURN_APPROVED,
        "approved",
        context={
            "order_number": return_request.order.number,
            "return_number": return_request.number,
        },
    )
    return return_request


@transaction.atomic
def reject_return_request(
    return_request: ReturnRequest,
    *,
    staff_user=None,
    reason: str = "",
) -> ReturnRequest:
    """Reject a customer return request."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    return_request.transition_to(
        ReturnRequest.Status.REJECTED,
        actor=staff_user,
        note=reason or _("Return rejected by staff."),
        metadata={"reason": reason},
    )

    _notify_return(
        return_request,
        NotificationType.RETURN_REJECTED,
        "rejected",
        context={
            "order_number": return_request.order.number,
            "return_number": return_request.number,
            "reason": reason,
        },
    )
    return return_request


# =============================================================================
# 4. Customer Cancellation
# =============================================================================


@transaction.atomic
def cancel_return_request(
    return_request: ReturnRequest,
    *,
    user,
    note: str = "",
) -> ReturnRequest:
    """Allow customer to cancel their own return while in REQUESTED or APPROVED state."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    if return_request.user_id != user.id and not getattr(user, "is_staff", False):
        raise ValidationError(_("You cannot cancel another user's return."))

    if not return_request.can_transition_to(ReturnRequest.Status.CANCELLED):
        raise InvalidTransition(
            "return request", return_request.status, ReturnRequest.Status.CANCELLED
        )

    return_request.transition_to(
        ReturnRequest.Status.CANCELLED,
        actor=user,
        note=note or _("Return cancelled by customer."),
    )
    return return_request


# =============================================================================
# 5. Warehouse Receipt
# =============================================================================


@transaction.atomic
def mark_return_received(
    return_request: ReturnRequest,
    *,
    staff_user=None,
    note: str = "",
    tracking_number: str = "",
) -> ReturnRequest:
    """Mark physical parcel received at the warehouse."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    if tracking_number:
        return_request.tracking_number = tracking_number
        return_request.save(update_fields=["tracking_number", "updated_at"])

    return_request.transition_to(
        ReturnRequest.Status.RECEIVED,
        actor=staff_user,
        note=note or _("Returned parcel received at warehouse."),
    )

    _notify_return(
        return_request,
        NotificationType.RETURN_RECEIVED,
        "received",
        context={
            "order_number": return_request.order.number,
            "return_number": return_request.number,
        },
    )
    return return_request


# =============================================================================
# 6. Physical Inspection
# =============================================================================


@transaction.atomic
def inspect_return(
    return_request: ReturnRequest,
    *,
    staff_user,
    inspections: list[dict[str, Any]],
    staff_note: str = "",
) -> ReturnRequest:
    """Record physical inspection findings.

    *inspections* is a list of dicts:
    [
        {
            "item_id": int,
            "received_quantity": int,
            "accepted_quantity": int,
            "rejected_quantity": int,
            "condition": str (unopened, like_new, etc.),
            "inspection_notes": str,
        },
        ...
    ]
    """
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status != ReturnRequest.Status.RECEIVED:
        raise ValidationError(_("Return must be in RECEIVED status before inspection."))

    total_accepted = 0
    total_rejected = 0

    items_by_id = {item.pk: item for item in return_request.items.select_for_update()}

    for insp in inspections:
        item_id = insp.get("item_id")
        item = items_by_id.get(item_id)
        if not item:
            raise ValidationError(
                _("Return item %(id)s does not belong to this return.") % {"id": item_id}
            )

        acc_qty = int(insp.get("accepted_quantity") or 0)
        rej_qty = int(insp.get("rejected_quantity") or 0)
        rec_raw = insp.get("received_quantity")
        rec_qty = acc_qty + rej_qty if rec_raw is None else int(rec_raw)

        if rec_qty > item.quantity:
            raise ValidationError(
                _("Received quantity (%(rec)s) cannot exceed requested quantity (%(req)s).")
                % {"rec": rec_qty, "req": item.quantity}
            )

        if acc_qty + rej_qty != rec_qty:
            raise ValidationError(
                _("Accepted quantity (%(acc)s) + rejected (%(rej)s) must equal received (%(rec)s).")
                % {"acc": acc_qty, "rej": rej_qty, "rec": rec_qty}
            )

        condition = insp.get("condition") or ReturnItem.Condition.LIKE_NEW
        if condition not in ReturnItem.Condition.values:
            condition = ReturnItem.Condition.LIKE_NEW

        item.received_quantity = rec_qty
        item.accepted_quantity = acc_qty
        item.rejected_quantity = rej_qty
        item.condition = condition
        item.inspection_notes = str(insp.get("inspection_notes") or "").strip()

        # Calculate refundable value for accepted units based on effective unit price
        effective_unit_price = item.order_item.line_total / Decimal(item.order_item.quantity)
        item.refund_amount = (effective_unit_price * Decimal(acc_qty)).quantize(Decimal("0.01"))
        item.save()

        total_accepted += acc_qty
        total_rejected += rej_qty

    return_request.staff_note = staff_note.strip()
    return_request.transition_to(
        ReturnRequest.Status.INSPECTED,
        actor=staff_user,
        note=_("Inspection complete: %(acc)s accepted, %(rej)s rejected.")
        % {"acc": total_accepted, "rej": total_rejected},
        metadata={"total_accepted": total_accepted, "total_rejected": total_rejected},
    )

    _notify_return(
        return_request,
        NotificationType.INSPECTION_COMPLETED,
        "inspected",
        context={
            "order_number": return_request.order.number,
            "return_number": return_request.number,
            "accepted_quantity": total_accepted,
            "rejected_quantity": total_rejected,
        },
    )

    return return_request


# =============================================================================
# 7. Refund Execution & Restocking
# =============================================================================


@transaction.atomic
def process_refund_for_return(
    return_request: ReturnRequest,
    *,
    staff_user,
    refund_shipping: bool = False,
    staff_note: str = "",
) -> Refund:
    """Issue payment refund and restock accepted units to authoritative inventory."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status != ReturnRequest.Status.INSPECTED:
        raise ValidationError(_("Return must be INSPECTED before issuing a refund."))

    if return_request.return_type != ReturnRequest.ReturnType.REFUND:
        raise ValidationError(_("This return request is marked for exchange, not refund."))

    # Sum accepted line items
    items = list(return_request.items.select_for_update())
    total_refund = sum((item.refund_amount for item in items), Decimal("0.00"))
    if refund_shipping:
        total_refund += return_request.order.shipping_amount

    if total_refund <= Decimal("0.00"):
        raise ValidationError(_("No accepted units to refund in this return."))

    # 1. Issue refund through payments system
    refund = process_refund(
        order=return_request.order,
        amount=total_refund,
        reason=Refund.Reason.RETURN,
        return_request=return_request,
        refund_shipping=refund_shipping,
        note=staff_note or f"Refund for return {return_request.number}",
        actor=staff_user,
        idempotency_key=return_request.number,
    )

    # 2. Authoritative inventory restock (prevent duplicate restock)
    for item in items:
        if item.accepted_quantity > 0 and not item.is_restocked:
            adjust_stock(
                item.order_item.variant,
                item.accepted_quantity,
                kind=InventoryMovement.Kind.RETURNED,
                reference=f"return:{return_request.number}",
                note=_("Restocked from return %(ret)s inspection.")
                % {"ret": return_request.number},
                user=staff_user,
            )
            item.is_restocked = True
            item.save(update_fields=["is_restocked"])

    # 3. Transition return request to REFUNDED
    return_request.transition_to(
        ReturnRequest.Status.REFUNDED,
        actor=staff_user,
        note=_("Refund %(num)s of %(amt)s %(curr)s processed.")
        % {
            "num": refund.number,
            "amt": refund.amount,
            "curr": return_request.order.currency,
        },
        metadata={"refund_number": refund.number, "amount": str(refund.amount)},
    )

    return refund


# =============================================================================
# 8. Exchange Fulfillment & Stock Movement
# =============================================================================


@transaction.atomic
def process_exchange_for_return(
    return_request: ReturnRequest,
    *,
    staff_user,
    staff_note: str = "",
) -> ReturnRequest:
    """Process exchange: restock accepted returned items and allocate replacement inventory."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status != ReturnRequest.Status.INSPECTED:
        raise ValidationError(_("Return must be INSPECTED before processing an exchange."))

    if return_request.return_type != ReturnRequest.ReturnType.EXCHANGE:
        raise ValidationError(_("This return request is not marked for exchange."))

    items = list(return_request.items.select_for_update())
    total_acc = sum(item.accepted_quantity for item in items)
    if total_acc <= 0:
        raise ValidationError(_("No accepted items to exchange."))

    # Restock original and allocate replacement stock
    for item in items:
        if item.accepted_quantity > 0 and not item.is_restocked:
            # 1. Restock original variant
            adjust_stock(
                item.order_item.variant,
                item.accepted_quantity,
                kind=InventoryMovement.Kind.RETURNED,
                reference=f"exchange_in:{return_request.number}",
                note=_("Restocked original variant from exchange %(ret)s.")
                % {"ret": return_request.number},
                user=staff_user,
            )

            # 2. Allocate replacement variant
            if item.replacement_variant:
                rep_stock = Stock.get_for_variant(item.replacement_variant)
                if rep_stock.available < item.accepted_quantity:
                    raise InsufficientStock(
                        item.replacement_variant.sku,
                        item.accepted_quantity,
                        rep_stock.available,
                    )
                adjust_stock(
                    item.replacement_variant,
                    -item.accepted_quantity,
                    kind=InventoryMovement.Kind.SOLD,
                    reference=f"exchange_out:{return_request.number}",
                    note=_("Allocated replacement variant for exchange %(ret)s.")
                    % {"ret": return_request.number},
                    user=staff_user,
                )

            item.is_restocked = True
            item.save(update_fields=["is_restocked"])

    # If original item cost more than replacement, refund the price difference
    price_diff_to_refund = Decimal("0.00")
    for item in items:
        if item.accepted_quantity > 0 and item.replacement_variant:
            unit_diff = item.order_item.unit_price - item.replacement_variant.price
            if unit_diff > Decimal("0.00"):
                price_diff_to_refund += unit_diff * Decimal(item.accepted_quantity)

    if price_diff_to_refund > Decimal("0.00"):
        process_refund(
            order=return_request.order,
            amount=price_diff_to_refund,
            reason=Refund.Reason.EXCHANGE_DIFFERENCE,
            return_request=return_request,
            note=f"Exchange price difference refund for return {return_request.number}",
            actor=staff_user,
            idempotency_key=f"exchange-{return_request.number}",
        )

    return_request.transition_to(
        ReturnRequest.Status.COMPLETED,
        actor=staff_user,
        note=staff_note or _("Exchange processed and replacement units allocated."),
    )

    _notify_return(
        return_request,
        NotificationType.EXCHANGE_PROCESSED,
        "exchange_processed",
        context={
            "order_number": return_request.order.number,
            "return_number": return_request.number,
        },
    )

    return return_request


# =============================================================================
# Helper: Notifications
# =============================================================================


def _notify_return(
    return_request: ReturnRequest,
    notification_type: str,
    key_suffix: str,
    *,
    context: dict[str, Any],
) -> None:
    """Emit customer notification tied to return lifecycle."""
    try:
        emit_notification(
            notification_type=notification_type,
            user=return_request.user,
            idempotency_key=f"return:{return_request.pk}:{key_suffix}",
            context=context,
        )
    except Exception as exc:
        logger.warning("Could not emit return notification %s: %s", notification_type, exc)
