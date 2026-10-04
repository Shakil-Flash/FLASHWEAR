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

from apps.orders.models import InvalidTransition, Order, OrderEvent

__all__ = ["OrderDetailView", "OrderListView", "order_cancel"]


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
            "items", "events", "shipments"
        ).select_related("payment")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        order = self.object
        context["events"] = list(order.events.all()[:20])
        context["shipment"] = order.shipments.first()
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
