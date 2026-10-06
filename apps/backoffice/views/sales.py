"""Sales screens: orders, payments, shipments, customers (Phase 16).

Reads come from :mod:`apps.backoffice.selectors.sales`; the *only* write on this screen
is :func:`apps.backoffice.services.operations.order_action`, which calls
``apps.orders.services`` and appends an audit row. Cancelling a paid order is deliberately
absent: that is a refund, and refunds are not this phase's business.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

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
from apps.orders.models import (
    InvalidTransition,
    Order,
    OrderEvent,
    ReturnItem,
    ReturnRequest,
    Shipment,
)
from apps.orders.returns_services import (
    approve_return_request,
    inspect_return,
    mark_return_received,
    process_exchange_for_return,
    process_refund_for_return,
    reject_return_request,
)
from apps.payments.models import Payment

__all__ = [
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
    )


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
    )


@require_POST
@backoffice_access(ORDERS_MANAGE)
def return_action(request, number: str):
    """Staff action on a return request: approve, reject, receive, inspect, refund, exchange."""
    return_qs = selectors.return_detail(number)
    return_request = get_object_or_404(return_qs)
    action = request.POST.get("action", "")

    try:
        if action == "approve":
            note = request.POST.get("note", "").strip()
            approve_return_request(return_request, staff_user=request.user, note=note)
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.approve",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
                reason=note,
            )
            messages.success(request, f"Return {return_request.number} approved.")

        elif action == "reject":
            reason = request.POST.get("reason", "").strip()
            if not reason:
                messages.error(request, "Please provide a reason for rejecting the return.")
                return redirect("backoffice:return_detail", number=number)
            reject_return_request(return_request, staff_user=request.user, reason=reason)
            audit_service.record(
                actor=request.user,
                domain="orders",
                action="return.reject",
                object_type="return_request",
                object_id=return_request.pk,
                object_repr=return_request.number,
                reason=reason,
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
                notes = request.POST.get(f"notes_{item.pk}", "").strip()
                inspections.append(
                    {
                        "item_id": item.pk,
                        "accepted_quantity": accepted,
                        "rejected_quantity": rejected,
                        "condition": cond,
                        "inspection_notes": notes,
                    }
                )
            staff_note = request.POST.get("staff_note", "").strip()
            inspect_return(
                return_request,
                staff_user=request.user,
                inspections=inspections,
                staff_note=staff_note,
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
