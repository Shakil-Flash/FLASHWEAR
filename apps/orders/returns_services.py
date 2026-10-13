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
from apps.catalog.validators import validate_catalog_image
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
    ReturnImage,
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
    "mark_return_awaiting_shipment",
    "mark_return_exchange_pending",
    "mark_return_inspection_failed",
    "mark_return_inspection_passed",
    "mark_return_received",
    "mark_return_refund_pending",
    "mark_return_under_review",
    "normalize_return_reason",
    "process_exchange_for_return",
    "process_refund_for_return",
    "record_manual_refund_for_return",
    "reject_return_request",
]


def generate_return_number() -> str:
    """Generate public handle: RET-<yyyymmdd>-<8 random chars>."""
    stamp = timezone.now().strftime("%Y%m%d")
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"RET-{stamp}-{suffix}"


def normalize_return_reason(val: str) -> str:
    """Normalize human/client strings to ReturnRequest.Reason choices."""
    if not val:
        return ReturnRequest.Reason.SIZE_FIT
    cleaned = str(val).strip().lower().replace(" ", "_").replace("-", "_")
    mapping = {
        "wrong_size": ReturnRequest.Reason.WRONG_SIZE,
        "size": ReturnRequest.Reason.WRONG_SIZE,
        "size_fit": ReturnRequest.Reason.SIZE_FIT,
        "size_or_fit_issue": ReturnRequest.Reason.SIZE_FIT,
        "damaged": ReturnRequest.Reason.DAMAGED,
        "damaged_item": ReturnRequest.Reason.DAMAGED,
        "defective": ReturnRequest.Reason.DEFECTIVE,
        "defective_product": ReturnRequest.Reason.DEFECTIVE_PRODUCT,
        "defective_or_damaged": ReturnRequest.Reason.DEFECTIVE,
        "incorrect": ReturnRequest.Reason.INCORRECT_ITEM,
        "incorrect_item": ReturnRequest.Reason.INCORRECT_ITEM,
        "wrong_item": ReturnRequest.Reason.WRONG_ITEM,
        "wrong_item_received": ReturnRequest.Reason.WRONG_ITEM,
        "changed_mind": ReturnRequest.Reason.CHANGED_MIND,
        "not_as_described": ReturnRequest.Reason.NOT_AS_DESCRIBED,
        "item_not_as_described": ReturnRequest.Reason.NOT_AS_DESCRIBED,
        "other": ReturnRequest.Reason.OTHER,
    }
    if cleaned in mapping:
        return mapping[cleaned]
    if val in ReturnRequest.Reason.values:
        return val
    return ReturnRequest.Reason.OTHER


# =============================================================================
# 1. Eligibility Check
# =============================================================================


def check_order_return_eligibility(order: Order) -> dict[str, Any]:
    """Calculate whether and to what extent *order* is eligible for a return/exchange.

    Rules enforced:
    1. Order must have been paid and delivered (or shipped) per configurable settings.
    2. Payment must have succeeded.
    3. Delivery must be within the configured return window (RETURN_WINDOW_DAYS).
    4. Units previously requested for return (and not rejected/cancelled) cannot be re-returned.
    5. Applicable product and category restrictions (e.g. underwear, swimwear, final-sale).
    6. At least one line item must have remaining returnable quantity > 0.
    """
    window_days = getattr(settings, "RETURN_WINDOW_DAYS", 30)
    eligible_statuses = getattr(
        settings, "RETURN_ELIGIBLE_ORDER_STATUSES", (Order.Status.DELIVERED, Order.Status.SHIPPED)
    )
    non_returnable_categories = [
        str(c).lower().strip()
        for c in getattr(
            settings,
            "NON_RETURNABLE_CATEGORIES",
            ["underwear", "swimwear", "earrings", "face-masks", "final-sale"],
        )
    ]
    non_returnable_tags = [
        str(t).lower().strip()
        for t in getattr(settings, "NON_RETURNABLE_TAGS", ["final-sale", "non-returnable"])
    ]

    # Status check
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

        # Product / category returnability check
        is_returnable_by_policy = True
        policy_restriction_reason = ""

        variant = line.variant
        if variant:
            if getattr(variant, "is_returnable", None) is False:
                is_returnable_by_policy = False
                policy_restriction_reason = _("This variant is marked non-returnable.")
            product = getattr(variant, "product", None)
            if product:
                if getattr(product, "is_returnable", None) is False:
                    is_returnable_by_policy = False
                    policy_restriction_reason = _("This product is marked non-returnable.")
                if product.category:
                    cat_slug = (product.category.slug or "").lower()
                    cat_name = (product.category.name or "").lower()
                    parent_slug = (
                        getattr(product.category.parent, "slug", "") or ""
                    ).lower()
                    if any(
                        c in (cat_slug, cat_name, parent_slug)
                        for c in non_returnable_categories
                    ):
                        is_returnable_by_policy = False
                        policy_restriction_reason = _(
                            "Items in this category are non-returnable for hygiene/policy reasons."
                        )

        # Check line product name for non-returnable tags
        prod_name_lower = line.product_name.lower()
        if any(tag in prod_name_lower for tag in non_returnable_tags):
            is_returnable_by_policy = False
            policy_restriction_reason = _("Item is marked as final sale / non-returnable.")

        is_item_eligible = remaining > 0 and is_returnable_by_policy
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
                "remaining_quantity": remaining if is_returnable_by_policy else 0,
                "is_eligible": is_item_eligible,
                "policy_restriction_reason": policy_restriction_reason,
            }
        )

    if not any_returnable:
        # Determine why none are returnable
        if all(not item.get("is_eligible") and item.get("policy_restriction_reason") for item in items_report):
            no_return_reason = _("Items on this order are non-returnable due to category or final-sale policy.")
        else:
            no_return_reason = _("All eligible items on this order have already been returned or requested.")
        return {
            "is_eligible": False,
            "reason": no_return_reason,
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
    images: list | None = None,
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
    *images* is an optional list of uploaded image files.
    """
    if order.user_id != user.id:
        raise ValidationError(_("You cannot request a return for another user's order."))

    if not items_data:
        raise ValidationError(_("Please select at least one item to return."))

    if return_type not in ReturnRequest.ReturnType.values:
        raise ValidationError(_("Invalid return type: %(type)s.") % {"type": return_type})

    reason = normalize_return_reason(reason)

    if images and len(images) > 5:
        raise ValidationError(_("You can upload at most 5 supporting images."))

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

    if images:
        for img in images:
            if not img:
                continue
            validate_catalog_image(img)
            ReturnImage.objects.create(
                return_request=return_request,
                image=img,
                caption="",
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
        if not elig_info or not elig_info.get("is_eligible") or qty > elig_info.get("remaining_quantity", 0):
            restriction = elig_info.get("policy_restriction_reason") if elig_info else ""
            remaining = elig_info["remaining_quantity"] if elig_info else 0
            if restriction:
                raise ValidationError(restriction)
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

        item_reason = normalize_return_reason(item_data.get("reason") or reason)

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
# 3. Staff Review, Approval & Rejection
# =============================================================================


@transaction.atomic
def mark_return_under_review(
    return_request: ReturnRequest,
    *,
    staff_user=None,
    note: str = "",
) -> ReturnRequest:
    """Move return request to UNDER_REVIEW."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    if staff_user and staff_user.pk == return_request.user_id:
        raise ValidationError(_("Staff members cannot review their own return requests."))

    return_request.transition_to(
        ReturnRequest.Status.UNDER_REVIEW,
        actor=staff_user,
        note=note or _("Return placed under staff review."),
    )
    return return_request


@transaction.atomic
def approve_return_request(
    return_request: ReturnRequest,
    *,
    staff_user=None,
    note: str = "",
    customer_explanation: str = "",
) -> ReturnRequest:
    """Approve a customer return request."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    if staff_user and staff_user.pk == return_request.user_id:
        raise ValidationError(_("Staff members cannot approve their own return requests."))

    if return_request.status == ReturnRequest.Status.APPROVED:
        # Idempotent re-approval
        return return_request

    if customer_explanation:
        return_request.customer_explanation = customer_explanation.strip()
        return_request.save(update_fields=["customer_explanation", "updated_at"])

    return_request.transition_to(
        ReturnRequest.Status.APPROVED,
        actor=staff_user,
        note=note or _("Return approved by staff."),
        metadata={"customer_explanation": customer_explanation} if customer_explanation else None,
    )

    _notify_return(
        return_request,
        NotificationType.RETURN_APPROVED,
        "approved",
        context={
            "order_number": return_request.order.number,
            "return_number": return_request.number,
            "customer_explanation": customer_explanation,
        },
    )
    return return_request


@transaction.atomic
def mark_return_awaiting_shipment(
    return_request: ReturnRequest,
    *,
    staff_user=None,
    note: str = "",
) -> ReturnRequest:
    """Move approved return request to AWAITING_SHIPMENT."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    return_request.transition_to(
        ReturnRequest.Status.AWAITING_SHIPMENT,
        actor=staff_user,
        note=note or _("Return awaiting customer shipment."),
    )
    return return_request


@transaction.atomic
def reject_return_request(
    return_request: ReturnRequest,
    *,
    staff_user=None,
    reason: str = "",
    customer_explanation: str = "",
) -> ReturnRequest:
    """Reject a customer return request."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    if staff_user and staff_user.pk == return_request.user_id:
        raise ValidationError(_("Staff members cannot reject their own return requests."))

    rejection_text = reason.strip() or customer_explanation.strip()
    if not rejection_text:
        raise ValidationError(_("A reason is required to reject a return request."))

    return_request.rejection_reason = rejection_text
    explanation = customer_explanation.strip() or rejection_text
    return_request.customer_explanation = explanation

    return_request.save(update_fields=["rejection_reason", "customer_explanation", "updated_at"])

    return_request.transition_to(
        ReturnRequest.Status.REJECTED,
        actor=staff_user,
        note=rejection_text or _("Return rejected by staff."),
        metadata={"reason": rejection_text, "customer_explanation": explanation},
    )

    _notify_return(
        return_request,
        NotificationType.RETURN_REJECTED,
        "rejected",
        context={
            "order_number": return_request.order.number,
            "return_number": return_request.number,
            "reason": rejection_text,
            "customer_explanation": explanation,
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
    """Allow customer to cancel their own return while in cancellable state."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    if return_request.user_id != user.id and not getattr(user, "is_staff", False):
        raise ValidationError(_("You cannot cancel another user's return."))

    if not return_request.is_cancellable_by_customer or not return_request.can_transition_to(
        ReturnRequest.Status.CANCELLED
    ):
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
# 6. Physical Inspection & Safety Outcomes
# =============================================================================


@transaction.atomic
def inspect_return(
    return_request: ReturnRequest,
    *,
    staff_user,
    inspections: list[dict[str, Any]],
    staff_note: str = "",
    customer_explanation: str = "",
    target_status: str | None = None,
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
            "disposition": str (optional: restockable, damaged, defective, disposal),
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

        # Determine disposition / safety outcome
        disposition = insp.get("disposition")
        if disposition not in ReturnItem.Disposition.values:
            if condition in (
                ReturnItem.Condition.UNOPENED,
                ReturnItem.Condition.LIKE_NEW,
                ReturnItem.Condition.OPENED_UNUSED,
            ):
                disposition = ReturnItem.Disposition.RESTOCKABLE
            elif condition == ReturnItem.Condition.DAMAGED_CUSTOMER:
                disposition = ReturnItem.Disposition.DAMAGED
            elif condition == ReturnItem.Condition.DEFECTIVE:
                disposition = ReturnItem.Disposition.DEFECTIVE
            elif condition == ReturnItem.Condition.WORN:
                disposition = ReturnItem.Disposition.DISPOSAL
            else:
                disposition = ReturnItem.Disposition.RESTOCKABLE

        item.received_quantity = rec_qty
        item.accepted_quantity = acc_qty
        item.rejected_quantity = rej_qty
        item.condition = condition
        item.disposition = disposition
        item.inspection_notes = str(insp.get("inspection_notes") or "").strip()

        # Calculate refundable value for accepted units based on effective unit price
        effective_unit_price = item.order_item.line_total / Decimal(item.order_item.quantity)
        item.refund_amount = (effective_unit_price * Decimal(acc_qty)).quantize(Decimal("0.01"))
        item.save()

        total_accepted += acc_qty
        total_rejected += rej_qty

    return_request.staff_note = staff_note.strip()
    if customer_explanation:
        return_request.customer_explanation = customer_explanation.strip()
        return_request.save(update_fields=["staff_note", "customer_explanation", "updated_at"])
    else:
        return_request.save(update_fields=["staff_note", "updated_at"])

    final_status = target_status or ReturnRequest.Status.INSPECTED
    if target_status is None:
        if total_accepted == 0 and total_rejected > 0 and staff_note:
            final_status = ReturnRequest.Status.INSPECTION_FAILED

    return_request.transition_to(
        final_status,
        actor=staff_user,
        note=_("Inspection complete: %(acc)s accepted, %(rej)s rejected.")
        % {"acc": total_accepted, "rej": total_rejected},
        metadata={
            "total_accepted": total_accepted,
            "total_rejected": total_rejected,
            "outcome": final_status,
        },
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
            "outcome": target_status,
        },
    )

    return return_request


@transaction.atomic
def mark_return_inspection_passed(
    return_request: ReturnRequest,
    *,
    staff_user,
    note: str = "",
) -> ReturnRequest:
    """Transition return request to INSPECTION_PASSED."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    return_request.transition_to(
        ReturnRequest.Status.INSPECTION_PASSED,
        actor=staff_user,
        note=note or _("Return inspection passed."),
    )
    return return_request


@transaction.atomic
def mark_return_inspection_failed(
    return_request: ReturnRequest,
    *,
    staff_user,
    reason: str = "",
    note: str = "",
) -> ReturnRequest:
    """Transition return request to INSPECTION_FAILED."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    rejection_text = reason.strip() or note.strip()
    if not rejection_text:
        raise ValidationError(_("A reason is required when inspection fails."))
    return_request.transition_to(
        ReturnRequest.Status.INSPECTION_FAILED,
        actor=staff_user,
        note=rejection_text,
        metadata={"reason": rejection_text},
    )
    return return_request


@transaction.atomic
def mark_return_refund_pending(
    return_request: ReturnRequest,
    *,
    staff_user=None,
    note: str = "",
) -> ReturnRequest:
    """Move return from INSPECTED or INSPECTION_PASSED to REFUND_PENDING."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    return_request.transition_to(
        ReturnRequest.Status.REFUND_PENDING,
        actor=staff_user,
        note=note or _("Inspection accepted; refund queued for processing."),
    )
    return return_request


@transaction.atomic
def mark_return_exchange_pending(
    return_request: ReturnRequest,
    *,
    staff_user=None,
    note: str = "",
) -> ReturnRequest:
    """Move return from INSPECTED or INSPECTION_PASSED to EXCHANGE_PENDING."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    return_request.transition_to(
        ReturnRequest.Status.EXCHANGE_PENDING,
        actor=staff_user,
        note=note or _("Inspection accepted; exchange fulfillment queued."),
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
    if return_request.status not in (
        ReturnRequest.Status.INSPECTED,
        ReturnRequest.Status.INSPECTION_PASSED,
        ReturnRequest.Status.REFUND_PENDING,
    ):
        raise ValidationError(_("Return must be INSPECTED before issuing a refund."))

    if return_request.return_type != ReturnRequest.ReturnType.REFUND:
        raise ValidationError(_("This return request is marked for exchange, not refund."))

    # Sum accepted line items
    items = list(return_request.items.select_for_update())
    total_refund = sum((item.refund_amount for item in items), Decimal("0.00"))

    # Shipping refund validation: only if not already refunded on previous refund
    if refund_shipping:
        already_refunded_shipping = return_request.order.refunds.filter(
            is_shipping_refunded=True,
            status=Refund.Status.SUCCEEDED,
        ).exists()
        if not already_refunded_shipping:
            total_refund += return_request.order.shipping_amount
        else:
            refund_shipping = False  # Shipping already refunded; do not double-refund

    if total_refund <= Decimal("0.00"):
        raise ValidationError(_("No accepted units to refund in this return."))

    # Cap to remaining refundable balance
    payment = Payment.objects.select_for_update().get(order=return_request.order)
    succeeded_refunds = payment.refunds.filter(status=Refund.Status.SUCCEEDED)
    total_already_refunded = sum((r.amount for r in succeeded_refunds), Decimal("0.00"))
    remaining_refundable = max(Decimal("0.00"), payment.amount - total_already_refunded)

    if total_refund > remaining_refundable:
        if remaining_refundable <= Decimal("0.00"):
            raise ValidationError(
                _("This order has already been fully refunded. No refundable balance remaining.")
            )
        total_refund = remaining_refundable

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

    # 2. Authoritative inventory restock: only items marked RESTOCKABLE
    for item in items:
        if item.accepted_quantity > 0 and not item.is_restocked:
            if item.disposition == ReturnItem.Disposition.RESTOCKABLE:
                adjust_stock(
                    item.order_item.variant,
                    item.accepted_quantity,
                    kind=InventoryMovement.Kind.RETURNED,
                    reference=f"return:{return_request.number}",
                    note=_("Restocked from return %(ret)s inspection.")
                    % {"ret": return_request.number},
                    user=staff_user,
                )
                item.restocked_quantity = item.accepted_quantity
            else:
                # Withheld from sellable inventory (damaged, defective, disposal)
                item.restocked_quantity = 0
                ReturnEvent.objects.create(
                    return_request=return_request,
                    event_type="withheld_from_stock",
                    actor=staff_user,
                    note=_(
                        "Item %(sku)s x%(qty)s disposition is %(disp)s; safely withheld from sellable inventory."
                    )
                    % {
                        "sku": item.order_item.sku,
                        "qty": item.accepted_quantity,
                        "disp": item.get_disposition_display(),
                    },
                    metadata={
                        "item_id": item.pk,
                        "sku": item.order_item.sku,
                        "disposition": item.disposition,
                    },
                )
            item.is_restocked = True
            item.save(update_fields=["is_restocked", "restocked_quantity"])

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


@transaction.atomic
def record_manual_refund_for_return(
    return_request: ReturnRequest,
    *,
    staff_user,
    amount: Decimal,
    reference: str = "",
    note: str = "",
) -> Refund:
    """Record a manually settled refund (e.g. offline bank transfer, cash, terminal)."""
    return_request = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status not in (
        ReturnRequest.Status.INSPECTED,
        ReturnRequest.Status.INSPECTION_PASSED,
        ReturnRequest.Status.REFUND_PENDING,
    ):
        raise ValidationError(_("Return must be inspected or refund pending to record refund."))

    order = Order.objects.select_for_update().get(pk=return_request.order_id)
    payment = Payment.objects.select_for_update().get(order=order)

    succeeded_refunds = payment.refunds.filter(status=Refund.Status.SUCCEEDED)
    total_already_refunded = sum((r.amount for r in succeeded_refunds), Decimal("0.00"))
    remaining_refundable = max(Decimal("0.00"), payment.amount - total_already_refunded)

    if amount <= Decimal("0.00"):
        raise ValidationError(_("Refund amount must be greater than zero."))
    if amount > remaining_refundable:
        raise ValidationError(
            _("Manual refund of %(amt)s exceeds remaining refundable balance of %(rem)s.")
            % {"amt": amount, "rem": remaining_refundable}
        )

    from apps.payments.services import generate_refund_number

    refund = Refund.objects.create(
        number=generate_refund_number(),
        order=order,
        payment=payment,
        return_request=return_request,
        amount=amount,
        currency=order.currency,
        status=Refund.Status.SUCCEEDED,
        reason=Refund.Reason.RETURN,
        provider="manual",
        provider_reference=reference or f"MANUAL-{return_request.number}",
        note=note or f"Manual refund recorded by {staff_user}",
        processed_at=timezone.now(),
    )

    # Restock eligible items if not already done
    for item in return_request.items.select_for_update():
        if item.accepted_quantity > 0 and not item.is_restocked:
            if item.disposition == ReturnItem.Disposition.RESTOCKABLE:
                adjust_stock(
                    item.order_item.variant,
                    item.accepted_quantity,
                    kind=InventoryMovement.Kind.RETURNED,
                    reference=f"return:{return_request.number}",
                    note=_("Restocked from manual refund %(ret)s.") % {"ret": return_request.number},
                    user=staff_user,
                )
                item.restocked_quantity = item.accepted_quantity
            else:
                item.restocked_quantity = 0
            item.is_restocked = True
            item.save(update_fields=["is_restocked", "restocked_quantity"])

    return_request.transition_to(
        ReturnRequest.Status.REFUNDED,
        actor=staff_user,
        note=note or f"Manual refund of {amount} {order.currency} recorded.",
        metadata={"refund_number": refund.number, "amount": str(amount), "manual": True},
    )

    _notify_return(
        return_request,
        NotificationType.PAYMENT_REFUNDED,
        "manual_refund",
        context={
            "order_number": order.number,
            "return_number": return_request.number,
            "amount": f"{amount} {order.currency}",
        },
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
    if return_request.status not in (
        ReturnRequest.Status.INSPECTED,
        ReturnRequest.Status.INSPECTION_PASSED,
        ReturnRequest.Status.EXCHANGE_PENDING,
    ):
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
            # 1. Restock original variant ONLY if restockable
            if item.disposition == ReturnItem.Disposition.RESTOCKABLE:
                adjust_stock(
                    item.order_item.variant,
                    item.accepted_quantity,
                    kind=InventoryMovement.Kind.RETURNED,
                    reference=f"exchange_in:{return_request.number}",
                    note=_("Restocked original variant from exchange %(ret)s.")
                    % {"ret": return_request.number},
                    user=staff_user,
                )
                item.restocked_quantity = item.accepted_quantity
            else:
                item.restocked_quantity = 0

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
            item.save(update_fields=["is_restocked", "restocked_quantity"])

    # If original item cost more than replacement, refund the price difference
    price_diff_to_refund = Decimal("0.00")
    price_diff_to_charge = Decimal("0.00")
    for item in items:
        if item.accepted_quantity > 0 and item.replacement_variant:
            unit_diff = item.order_item.unit_price - item.replacement_variant.price
            if unit_diff > Decimal("0.00"):
                price_diff_to_refund += unit_diff * Decimal(item.accepted_quantity)
            elif unit_diff < Decimal("0.00"):
                price_diff_to_charge += (-unit_diff) * Decimal(item.accepted_quantity)

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

    if price_diff_to_charge > Decimal("0.00"):
        ReturnEvent.objects.create(
            return_request=return_request,
            event_type="price_difference_pending",
            actor=staff_user,
            note=_(
                "Replacement variant has an additional cost of %(amt)s %(curr)s to be collected."
            )
            % {"amt": price_diff_to_charge, "curr": return_request.order.currency},
            metadata={"price_diff_to_charge": str(price_diff_to_charge)},
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
