"""Customer-facing order screens, mounted under ``/account/orders/``.

The URL patterns live in :mod:`apps.accounts.account_urls` (the account area
declares exactly one ``app_name``), the views live here with the rest of the
order code. Every queryset is scoped to ``request.user`` in the query itself, so
an order number from another account simply 404s.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from apps.orders.models import InvalidTransition, Order, OrderEvent, ReturnRequest
from apps.orders.returns_services import (
    cancel_return_request,
    check_order_return_eligibility,
    create_return_request,
)

__all__ = [
    "CustomerReturnDetailView",
    "CustomerReturnListView",
    "OrderDetailView",
    "OrderListView",
    "order_cancel",
    "order_return_request",
    "return_cancel",
]


class OrderListView(ListView):
    """Order history: number, date, status, total -- newest first."""

    template_name = "account/order_list.html"
    context_object_name = "orders"
    paginate_by = 10

    def get_queryset(self):
        return (
            self.request.user.orders.select_related("payment")
            .prefetch_related("items")
            .order_by("-created_at")
        )


class OrderDetailView(DetailView):
    """One order: lines, address, totals and the event timeline."""

    template_name = "account/order_detail.html"
    context_object_name = "order"
    slug_field = "number"
    slug_url_kwarg = "number"

    def get_queryset(self):
        return self.request.user.orders.prefetch_related(
            "items", "events", "shipments", "returns"
        ).select_related("payment")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        order = self.object
        context["events"] = list(order.events.all()[:20])
        context["shipment"] = order.shipments.first()
        context["return_eligibility"] = check_order_return_eligibility(order)
        context["order_returns"] = list(order.returns.all())
        return context


@require_POST
def order_cancel(request, number: str):
    """Cancel an unpaid order: payment (if any) first, then stock release."""
    from apps.payments.services import cancel_payment

    order = get_object_or_404(Order, number=number, user=request.user)

    if not order.is_cancellable:
        messages.error(
            request, _("This order can no longer be cancelled. Contact support if you need help.")
        )
        return redirect("account:order-detail", number=order.number)

    payment = getattr(order, "payment", None)
    try:
        if payment is not None:
            # Cancels the payment and, with it, the order and its stock holds.
            cancel_payment(payment, actor=request.user, reason="Cancelled by the customer.")
        else:
            from apps.orders.services import cancel_order

            cancel_order(
                order,
                actor=request.user,
                note="Cancelled by the customer.",
                event_type=OrderEvent.Type.CANCELLED_BY_CUSTOMER,
            )
    except InvalidTransition:
        messages.error(request, _("This order can no longer be cancelled."))
        return redirect("account:order-detail", number=order.number)

    messages.success(request, _("Order %(number)s was cancelled.") % {"number": order.number})
    return redirect("account:order-detail", number=order.number)


def order_return_request(request, number: str):
    """Customer creates a return or exchange request for an eligible order."""
    from django.core.exceptions import ValidationError
    from django.shortcuts import render

    order = get_object_or_404(
        request.user.orders.prefetch_related("items__variant__product__variants"),
        number=number,
    )
    eligibility = check_order_return_eligibility(order)
    if not eligibility["is_eligible"]:
        messages.error(
            request,
            eligibility["reason"] or _("This order is not eligible for returns or exchanges."),
        )
        return redirect("account:order-detail", number=order.number)

    # Pre-calculate available exchange variants for each eligible line item
    exchange_variants_by_item: dict[int, list[dict]] = {}
    for item in order.items.all():
        if item.variant:
            siblings = (
                item.variant.product.variants.filter(is_active=True)
                .exclude(pk=item.variant_id)
                .select_related("product")
            )
            exchange_variants_by_item[item.pk] = [
                {
                    "id": sib.pk,
                    "sku": sib.sku,
                    "label": f"{sib.option_label or sib.sku} ({sib.price})",
                    "price": str(sib.price),
                }
                for sib in siblings
            ]
        else:
            exchange_variants_by_item[item.pk] = []

    if request.method == "POST":
        return_type = request.POST.get("return_type", ReturnRequest.ReturnType.REFUND)
        reason = request.POST.get("reason", ReturnRequest.Reason.SIZE_FIT)
        customer_note = request.POST.get("customer_note", "")

        items_data = []
        for line in order.items.all():
            if f"item_{line.pk}_selected" in request.POST:
                try:
                    qty = int(request.POST.get(f"item_{line.pk}_quantity", 1))
                except (ValueError, TypeError):
                    qty = 1

                replacement_id = request.POST.get(f"item_{line.pk}_replacement_variant")
                items_data.append(
                    {
                        "order_item_id": line.pk,
                        "quantity": qty,
                        "reason": request.POST.get(f"item_{line.pk}_reason", reason),
                        "customer_note": request.POST.get(f"item_{line.pk}_note", ""),
                        "replacement_variant_id": int(replacement_id) if replacement_id else None,
                    }
                )

        if not items_data:
            messages.error(request, _("Please select at least one item to return or exchange."))
            return render(
                request,
                "account/return_request.html",
                {
                    "order": order,
                    "eligibility": eligibility,
                    "reasons": ReturnRequest.Reason.choices,
                    "return_types": ReturnRequest.ReturnType.choices,
                    "exchange_variants_by_item": exchange_variants_by_item,
                },
            )

        try:
            return_obj = create_return_request(
                order=order,
                user=request.user,
                items_data=items_data,
                return_type=return_type,
                reason=reason,
                customer_note=customer_note,
            )
            messages.success(
                request,
                _("Return request %(number)s submitted successfully.")
                % {"number": return_obj.number},
            )
            return redirect("account:return-detail", number=return_obj.number)
        except ValidationError as exc:
            msg = exc.messages if hasattr(exc, "messages") else [str(exc)]
            messages.error(request, " ".join(msg))

    return render(
        request,
        "account/return_request.html",
        {
            "order": order,
            "eligibility": eligibility,
            "reasons": ReturnRequest.Reason.choices,
            "return_types": ReturnRequest.ReturnType.choices,
            "exchange_variants_by_item": exchange_variants_by_item,
        },
    )


class CustomerReturnListView(ListView):
    """Customer-facing return request history."""

    template_name = "account/return_list.html"
    context_object_name = "returns"
    paginate_by = 10

    def get_queryset(self):
        return (
            self.request.user.returns.select_related("order")
            .prefetch_related("items__order_item", "refunds")
            .order_by("-created_at")
        )


class CustomerReturnDetailView(DetailView):
    """Customer-facing single return request detail with items, events, and refund status."""

    template_name = "account/return_detail.html"
    context_object_name = "return_request"
    slug_field = "number"
    slug_url_kwarg = "number"

    def get_queryset(self):
        return self.request.user.returns.select_related("order").prefetch_related(
            "items__order_item",
            "items__replacement_variant__product",
            "events",
            "refunds",
        )


@require_POST
def return_cancel(request, number: str):
    """Customer cancels their return request while in REQUESTED status."""
    return_request = get_object_or_404(request.user.returns, number=number)

    if not return_request.is_cancellable_by_customer:
        messages.error(
            request, _("This return request cannot be cancelled once processed or closed.")
        )
        return redirect("account:return-detail", number=return_request.number)

    note = request.POST.get("note", "Cancelled by customer")
    try:
        cancel_return_request(return_request, user=request.user, note=note)
    except InvalidTransition:
        messages.error(request, _("This return request can no longer be cancelled."))
        return redirect("account:return-detail", number=return_request.number)

    messages.success(
        request,
        _("Return request %(number)s was cancelled.") % {"number": return_request.number},
    )
    return redirect("account:return-detail", number=return_request.number)
