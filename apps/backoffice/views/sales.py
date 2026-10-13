"""Sales screens: orders, payments, shipments, customers (Phase 16).

Reads come from :mod:`apps.backoffice.selectors.sales`; the *only* write on this screen
is :func:`apps.backoffice.services.operations.order_action`, which calls
``apps.orders.services`` and appends an audit row. Cancelling a paid order is deliberately
absent: that is a refund, and refunds are not this phase's business.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from decimal import Decimal

from apps.backoffice import selectors
from apps.backoffice.forms import OrderActionForm, OrderNoteForm
from apps.backoffice.permissions import (
    CUSTOMERS_VIEW,
    ORDERS_MANAGE,
    ORDERS_VIEW,
    PAYMENTS_VIEW,
    backoffice_access,
    has,
)
from apps.backoffice.services import audit as audit_service
from apps.backoffice.services import operations
from apps.backoffice.views.base import range_from_request, render_bo
from apps.orders.fulfillment_services import (
    cancel_fulfillment,
    create_fulfillment,
    dispatch_shipment,
    get_order_fulfillment_summary,
    get_unfulfilled_order_items,
    mark_packed,
    record_delivery_exception,
    start_picking,
    update_tracking_event,
)
from apps.orders.models import (
    InvalidTransition,
    Order,
    OrderEvent,
    ReturnItem,
    ReturnRequest,
    Shipment,
    ShipmentEvent,
    ShipmentItem,
)
from apps.orders.returns_services import (
    approve_return_request,
    inspect_return,
    mark_return_awaiting_shipment,
    mark_return_received,
    mark_return_under_review,
    process_exchange_for_return,
    process_refund_for_return,
    record_manual_refund_for_return,
    reject_return_request,
)
from apps.payments.models import Payment

__all__ = [
    "create_order_fulfillment",
    "customers",
    "order_action",
    "order_detail",
    "order_note",
    "orders",
    "payment_detail",
    "payments",
    "return_action",
    "return_detail",
    "returns",
    "shipment_action",
    "shipment_detail",
    "shipments",
]


@backoffice_access(ORDERS_VIEW)
def orders(request):
    """``/operations/orders/`` -- the queue, filtered by allowlisted values only."""
    date_range = range_from_request(request)
    queryset = selectors.orders(
        q=request.GET.get("q", ""),
        status=request.GET.get("status", ""),
        date_range=date_range,
        sort=request.GET.get("sort", ""),
    )
    return render_bo(
        request,
        "backoffice/orders/list.html",
        active="orders",
        page=selectors.paginate(request, queryset),
        current_range=date_range.key,
        filters={
            "q": request.GET.get("q", ""),
            "status": request.GET.get("status", ""),
            "sort": request.GET.get("sort", ""),
        },
        status_choices=Order.Status.choices,
        can_manage=has(request.user, ORDERS_MANAGE),
    )


def _can(request, capability: str) -> bool:
    """Capability of the current user (kept small so templates never call ``has``)."""
    return has(request.user, capability)


@backoffice_access(ORDERS_VIEW)
def order_detail(request, number: str):
    """``/operations/orders/<number>/`` -- facts, timeline and the available transitions."""
    order = get_object_or_404(selectors.order_detail(number))
    return render_bo(
        request,
        "backoffice/orders/detail.html",
        active="orders",
        order=order,
        timeline=selectors.order_timeline(order),
        action_form=OrderActionForm(),
        note_form=OrderNoteForm(),
        can_manage=_can(request, ORDERS_MANAGE),
        events=selectors.order_events(order),
        fulfillment_summary=get_order_fulfillment_summary(order),
    )


@require_POST
@backoffice_access(ORDERS_MANAGE)
def order_action(request, number: str):
    """Advance or cancel an order. POST-only: a GET never changes state."""
    order = get_object_or_404(Order.objects.filter(number=number))
    form = OrderActionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
        return redirect("backoffice:order_detail", number=number)

    data = form.cleaned_data
    try:
        operations.order_action(
            order=order,
            action=data["action"],
            actor=request.user,
            note=data["note"],
            tracking_number=data["tracking_number"],
            carrier=data["carrier"],
        )
    except operations.DOMAIN_ERRORS as exc:
        messages.error(request, operations.error_message(exc))
    else:
        messages.success(
            request, f"Order {order.number} is now {order.get_status_display().lower()}."
        )
    return redirect("backoffice:order_detail", number=number)


@require_POST
@backoffice_access(ORDERS_MANAGE)
def order_note(request, number: str):
    """Append an internal note to the timeline (no state change)."""
    order = get_object_or_404(Order.objects.filter(number=number))
    form = OrderNoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "The note could not be saved.")
        return redirect("backoffice:order_detail", number=number)

    note = form.cleaned_data["note"].strip()
    with transaction.atomic():
        OrderEvent.objects.create(
            order=order,
            event_type=OrderEvent.Type.NOTE,
            actor=request.user,
            note=note,
        )
        audit_service.record(
            actor=request.user,
            domain="orders",
            action="order.note",
            object_type="orders.order",
            object_id=order.number,
            object_repr=str(order),
            reason=note[:255],
        )
    messages.success(request, "Note added.")
    return redirect("backoffice:order_detail", number=number)


@backoffice_access(PAYMENTS_VIEW)
def payments(request):
    """``/operations/payments/`` -- state and reference, never the provider payload."""
    date_range = range_from_request(request)
    queryset = selectors.payments(
        q=request.GET.get("q", ""),
        status=request.GET.get("status", ""),
        date_range=date_range,
        sort=request.GET.get("sort", ""),
    )
    return render_bo(
        request,
        "backoffice/payments/list.html",
        active="payments",
        page=selectors.paginate(request, queryset),
        current_range=date_range.key,
        filters={
            "q": request.GET.get("q", ""),
            "status": request.GET.get("status", ""),
            "sort": request.GET.get("sort", ""),
        },
        status_choices=Payment.Status.choices,
    )


@backoffice_access(PAYMENTS_VIEW)
def payment_detail(request, pk: int):
    """``/operations/payments/<pk>/`` -- provider events with their payloads withheld."""
    payment = get_object_or_404(selectors.payment_detail(pk))
    return render_bo(
        request,
        "backoffice/payments/detail.html",
        active="payments",
        payment=payment,
        events=selectors.payment_events(payment),
    )


@backoffice_access(ORDERS_VIEW)
def shipments(request):
    """``/operations/shipments/`` -- the fulfilment queue."""
    date_range = range_from_request(request)
    queryset = selectors.shipments(
        q=request.GET.get("q", ""),
        status=request.GET.get("status", ""),
        date_range=date_range,
        sort=request.GET.get("sort", ""),
    )
    return render_bo(
        request,
        "backoffice/shipments.html",
        active="shipments",
        page=selectors.paginate(request, queryset),
        current_range=date_range.key,
        filters={
            "q": request.GET.get("q", ""),
            "status": request.GET.get("status", ""),
            "sort": request.GET.get("sort", ""),
        },
        status_choices=Shipment.Status.choices,
        can_manage=_can(request, ORDERS_MANAGE),
    )


@backoffice_access(ORDERS_VIEW)
def shipment_detail(request, reference: str):
    """``/operations/shipments/<reference>/`` -- package lines, tracking scans, and operational controls."""
    shipment = get_object_or_404(
        Shipment.objects.select_related("order", "order__user", "picked_by", "packed_by", "dispatched_by")
        .prefetch_related("items__order_item", "events__actor"),
        models.Q(reference=reference) | models.Q(pk=int(reference) if reference.isdigit() else 0),
    )
    unfulfilled_summary = get_order_fulfillment_summary(shipment.order)
    return render_bo(
        request,
        "backoffice/shipments/detail.html",
        active="shipments",
        shipment=shipment,
        order=shipment.order,
        items=list(shipment.items.select_related("order_item").all()),
        events=list(shipment.events.all()),
        unfulfilled_summary=unfulfilled_summary,
        status_choices=Shipment.Status.choices,
        can_manage=_can(request, ORDERS_MANAGE),
    )


@require_POST
@backoffice_access(ORDERS_MANAGE)
def shipment_action(request, reference: str):
    """Execute operational workflow step on a shipment package."""
    shipment = get_object_or_404(
        Shipment.objects.select_related("order"),
        models.Q(reference=reference) | models.Q(pk=int(reference) if reference.isdigit() else 0),
    )
    action = request.POST.get("action", "").strip()
    note = request.POST.get("note", "").strip()

    try:
        if action == "start_picking":
            start_picking(shipment, staff_user=request.user, note=note)
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="shipment.picking",
                object_type="shipment",
                object_id=shipment.pk,
                object_repr=shipment.reference or f"#{shipment.pk}",
            )
            messages.success(request, f"Shipment {shipment.reference or shipment.pk} is now Picking.")

        elif action == "mark_packed":
            raw_weight = request.POST.get("package_weight_kg", "").strip()
            dimensions = request.POST.get("package_dimensions", "").strip()
            internal_notes = request.POST.get("internal_notes", "").strip()
            customer_notes = request.POST.get("customer_notes", "").strip()
            weight_kg = Decimal(raw_weight) if raw_weight else None
            mark_packed(
                shipment,
                staff_user=request.user,
                package_weight_kg=weight_kg,
                package_dimensions=dimensions,
                internal_notes=internal_notes,
                customer_notes=customer_notes,
                note=note,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="shipment.packed",
                object_type="shipment",
                object_id=shipment.pk,
                object_repr=shipment.reference or f"#{shipment.pk}",
            )
            messages.success(request, f"Shipment {shipment.reference or shipment.pk} marked as Packed.")

        elif action == "dispatch":
            carrier = request.POST.get("carrier", "").strip()
            service = request.POST.get("carrier_service", "").strip()
            tracking_number = request.POST.get("tracking_number", "").strip()
            tracking_url = request.POST.get("tracking_url", "").strip()
            raw_est = request.POST.get("estimated_delivery", "").strip()
            est_date = None
            if raw_est:
                from django.utils.dateparse import parse_date
                est_date = parse_date(raw_est)

            dispatch_shipment(
                shipment,
                staff_user=request.user,
                carrier=carrier,
                carrier_service=service,
                tracking_number=tracking_number,
                tracking_url=tracking_url,
                estimated_delivery=est_date,
                note=note,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="shipment.dispatched",
                object_type="shipment",
                object_id=shipment.pk,
                object_repr=shipment.reference or f"#{shipment.pk}",
                metadata={"carrier": carrier, "tracking_number": tracking_number},
            )
            messages.success(request, f"Shipment {shipment.reference or shipment.pk} Dispatched.")

        elif action == "update_status":
            target_status = request.POST.get("target_status", "").strip()
            location = request.POST.get("location", "").strip()
            update_tracking_event(
                shipment,
                event_type=target_status,
                status=target_status,
                location=location,
                note=note,
                actor=request.user,
                source=ShipmentEvent.Source.STAFF,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="shipment.tracking_event",
                object_type="shipment",
                object_id=shipment.pk,
                object_repr=shipment.reference or f"#{shipment.pk}",
                metadata={"target_status": target_status, "location": location},
            )
            messages.success(request, f"Tracking checkpoint updated for shipment {shipment.reference or shipment.pk}.")

        elif action == "exception":
            exc_type = request.POST.get("exception_type", Shipment.Status.EXCEPTION).strip()
            reason = request.POST.get("reason", "").strip()
            location = request.POST.get("location", "").strip()
            record_delivery_exception(
                shipment,
                staff_user=request.user,
                exception_type=exc_type,
                reason=reason,
                location=location,
                note=note,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="shipment.exception",
                object_type="shipment",
                object_id=shipment.pk,
                object_repr=shipment.reference or f"#{shipment.pk}",
                metadata={"exception_type": exc_type, "reason": reason},
            )
            messages.warning(request, f"Delivery exception recorded for shipment {shipment.reference or shipment.pk}.")

        elif action == "cancel":
            reason = request.POST.get("reason", "").strip()
            cancel_fulfillment(shipment, staff_user=request.user, reason=reason)
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="shipment.cancel",
                object_type="shipment",
                object_id=shipment.pk,
                object_repr=shipment.reference or f"#{shipment.pk}",
                reason=reason,
            )
            messages.success(request, f"Shipment {shipment.reference or shipment.pk} cancelled.")

        elif action == "add_note":
            internal = request.POST.get("internal_note", "").strip()
            customer = request.POST.get("customer_note", "").strip()
            if internal:
                shipment.internal_notes = (shipment.internal_notes + "\n" + internal).strip()
            if customer:
                shipment.customer_notes = customer
            shipment.save(update_fields=["internal_notes", "customer_notes", "updated_at"])
            messages.success(request, "Notes updated.")

        else:
            messages.error(request, f"Unknown action: {action}")

    except (ValidationError, InvalidTransition) as exc:
        msg = exc.messages if hasattr(exc, "messages") else [str(exc)]
        messages.error(request, " ".join(msg))

    ref = shipment.reference or str(shipment.pk)
    return redirect("backoffice:shipment_detail", reference=ref)


@require_POST
@backoffice_access(ORDERS_MANAGE)
def create_order_fulfillment(request, number: str):
    """Create a fulfillment package (or split package) for selected items on an order."""
    order = get_object_or_404(Order.objects.filter(number=number))
    carrier = request.POST.get("carrier", "").strip()
    service = request.POST.get("carrier_service", "").strip()
    tracking_number = request.POST.get("tracking_number", "").strip()

    items_data = []
    for item in order.items.all():
        raw_qty = request.POST.get(f"qty_{item.pk}", "").strip()
        if raw_qty and raw_qty.isdigit() and int(raw_qty) > 0:
            items_data.append({"order_item_id": item.pk, "quantity": int(raw_qty)})

    try:
        shipment = create_fulfillment(
            order=order,
            items_data=items_data if items_data else None,
            staff_user=request.user,
            carrier=carrier,
            carrier_service=service,
            tracking_number=tracking_number,
        )
        audit_service.record(
            actor=request.user,
            domain="orders",
            action="order.fulfillment_create",
            object_type="orders.order",
            object_id=order.number,
            object_repr=str(order),
            metadata={"shipment": shipment.reference or shipment.pk},
        )
        messages.success(request, f"Fulfillment package {shipment.reference or shipment.pk} created.")
        return redirect("backoffice:shipment_detail", reference=shipment.reference or str(shipment.pk))
    except (ValidationError, InvalidTransition) as exc:
        msg = exc.messages if hasattr(exc, "messages") else [str(exc)]
        messages.error(request, " ".join(msg))
        return redirect("backoffice:order_detail", number=number)


@backoffice_access(CUSTOMERS_VIEW)
def customers(request):
    """``/operations/customers/`` -- search with counters, no customer content."""
    date_range = range_from_request(request)
    queryset = selectors.customer_search(
        q=request.GET.get("q", ""),
        date_range=date_range,
        sort=request.GET.get("sort", ""),
    )
    return render_bo(
        request,
        "backoffice/customers.html",
        active="customers",
        page=selectors.paginate(request, queryset),
        current_range=date_range.key,
        filters={
            "q": request.GET.get("q", ""),
            "sort": request.GET.get("sort", ""),
        },
    )


@backoffice_access(ORDERS_VIEW)
def returns(request):
    """``/operations/returns/`` -- returns queue, filtered and paginated."""
    date_range = range_from_request(request)
    queryset = selectors.returns(
        q=request.GET.get("q", ""),
        status=request.GET.get("status", ""),
        return_type=request.GET.get("return_type", ""),
        date_range=date_range,
        sort=request.GET.get("sort", ""),
    )
    return render_bo(
        request,
        "backoffice/returns/list.html",
        active="returns",
        page=selectors.paginate(request, queryset),
        current_range=date_range.key,
        filters={
            "q": request.GET.get("q", ""),
            "status": request.GET.get("status", ""),
            "return_type": request.GET.get("return_type", ""),
            "sort": request.GET.get("sort", ""),
        },
        status_choices=ReturnRequest.Status.choices,
        type_choices=ReturnRequest.ReturnType.choices,
        can_manage=has(request.user, ORDERS_MANAGE),
    )


@backoffice_access(ORDERS_VIEW)
def return_detail(request, number: str):
    """``/operations/returns/<number>/`` -- details, items inspection, actions."""
    return_qs = selectors.return_detail(number)
    return_request = get_object_or_404(return_qs)
    return render_bo(
        request,
        "backoffice/returns/detail.html",
        active="returns",
        return_request=return_request,
        can_manage=has(request.user, ORDERS_MANAGE),
        condition_choices=ReturnItem.Condition.choices,
        disposition_choices=ReturnItem.Disposition.choices,
    )


@require_POST
@backoffice_access(ORDERS_MANAGE)
def return_action(request, number: str):
    """Staff action on a return request: approve, reject, receive, inspect, refund, exchange."""
    return_qs = selectors.return_detail(number)
    return_request = get_object_or_404(return_qs)
    action = request.POST.get("action", "")

    try:
        if action == "under_review":
            note = request.POST.get("note", "").strip()
            mark_return_under_review(return_request, staff_user=request.user, note=note)
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.under_review",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
                reason=note,
            )
            messages.success(request, f"Return {return_request.number} moved to Under Review.")

        elif action == "approve":
            note = request.POST.get("note", "").strip()
            customer_explanation = request.POST.get("customer_explanation", "").strip()
            approve_return_request(
                return_request,
                staff_user=request.user,
                note=note,
                customer_explanation=customer_explanation,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.approve",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
                reason=note or customer_explanation,
            )
            messages.success(request, f"Return {return_request.number} approved.")

        elif action == "awaiting_shipment":
            note = request.POST.get("note", "").strip()
            mark_return_awaiting_shipment(return_request, staff_user=request.user, note=note)
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.awaiting_shipment",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
                reason=note,
            )
            messages.success(request, f"Return {return_request.number} marked as Awaiting Shipment.")

        elif action == "reject":
            reason = request.POST.get("reason", "").strip()
            customer_explanation = request.POST.get("customer_explanation", "").strip()
            if not reason and not customer_explanation:
                messages.error(request, "Please provide a reason for rejecting the return.")
                return redirect("backoffice:return_detail", number=number)
            rejection_text = reason or customer_explanation
            reject_return_request(
                return_request,
                staff_user=request.user,
                reason=rejection_text,
                customer_explanation=customer_explanation or rejection_text,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.reject",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
                reason=rejection_text,
            )
            messages.success(request, f"Return {return_request.number} rejected.")

        elif action == "receive":
            tracking = request.POST.get("tracking_number", "").strip()
            note = request.POST.get("note", "").strip()
            mark_return_received(
                return_request,
                staff_user=request.user,
                note=note,
                tracking_number=tracking,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.receive",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
                metadata={"tracking_number": tracking},
            )
            messages.success(request, f"Return {return_request.number} marked as received.")

        elif action == "inspect":
            inspections = []
            for item in return_request.items.all():
                accepted = int(request.POST.get(f"accepted_{item.pk}", 0) or 0)
                rejected = int(request.POST.get(f"rejected_{item.pk}", 0) or 0)
                cond = request.POST.get(f"condition_{item.pk}", ReturnItem.Condition.LIKE_NEW)
                disp = request.POST.get(f"disposition_{item.pk}", "")
                notes = request.POST.get(f"notes_{item.pk}", "").strip()
                insp_dict = {
                    "item_id": item.pk,
                    "accepted_quantity": accepted,
                    "rejected_quantity": rejected,
                    "condition": cond,
                    "inspection_notes": notes,
                }
                if disp:
                    insp_dict["disposition"] = disp
                inspections.append(insp_dict)
            staff_note = request.POST.get("staff_note", "").strip()
            customer_explanation = request.POST.get("customer_explanation", "").strip()
            inspect_return(
                return_request,
                staff_user=request.user,
                inspections=inspections,
                staff_note=staff_note,
                customer_explanation=customer_explanation,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.inspect",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
                metadata={"inspected_items": len(inspections)},
            )
            messages.success(request, f"Return {return_request.number} inspection recorded.")

        elif action == "refund":
            refund_shipping = request.POST.get("refund_shipping") == "1"
            staff_note = request.POST.get("note", "").strip()
            process_refund_for_return(
                return_request,
                staff_user=request.user,
                refund_shipping=refund_shipping,
                staff_note=staff_note,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.refund",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
                metadata={"refund_shipping": refund_shipping},
            )
            messages.success(request, f"Refund completed for return {return_request.number}.")

        elif action == "manual_refund":
            raw_amount = request.POST.get("amount", "").strip()
            ref = request.POST.get("reference", "").strip()
            staff_note = request.POST.get("note", "").strip()
            try:
                amt = Decimal(raw_amount)
            except Exception:
                messages.error(request, "Please enter a valid numeric refund amount.")
                return redirect("backoffice:return_detail", number=number)

            record_manual_refund_for_return(
                return_request,
                staff_user=request.user,
                amount=amt,
                reference=ref,
                note=staff_note,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.manual_refund",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
                metadata={"amount": str(amt), "reference": ref},
            )
            messages.success(
                request,
                f"Manual refund of {amt} {return_request.order.currency} recorded for return {return_request.number}.",
            )

        elif action == "exchange":
            staff_note = request.POST.get("note", "").strip()
            process_exchange_for_return(
                return_request,
                staff_user=request.user,
                staff_note=staff_note,
            )
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.exchange",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
            )
            messages.success(request, f"Exchange completed for return {return_request.number}.")

        else:
            messages.error(request, f"Unknown action: {action}")

    except (ValidationError, InvalidTransition) as exc:
        msg = exc.messages if hasattr(exc, "messages") else [str(exc)]
        messages.error(request, " ".join(msg))

    return redirect("backoffice:return_detail", number=number)
