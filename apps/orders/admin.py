"""Orders admin: read-only facts, deliberate transitions, fulfilment actions.

Nothing on the order form is a free-text state: status, money and timestamps are
read-only, and every change goes through a named action that calls the same
service the webhook or the customer would. That is what keeps the admin screen
from becoming a second, quieter state machine.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _

from apps.orders import services
from apps.orders.models import (
    InvalidTransition,
    Order,
    OrderAddress,
    OrderEvent,
    OrderItem,
    Shipment,
    ShipmentEvent,
)

__all__ = ["OrderAdmin", "ShipmentAdmin"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    fields = ("sku", "product_name", "option_label", "quantity", "unit_price", "line_total")
    readonly_fields = fields
    ordering = ("pk",)


class OrderAddressInline(admin.StackedInline):
    model = OrderAddress
    max_num = 1
    can_delete = False
    fields = (
        "full_name",
        "phone",
        "line1",
        "line2",
        "city",
        "region",
        "postal_code",
        "country",
        "address",
    )
    readonly_fields = fields


class OrderEventInline(admin.TabularInline):
    model = OrderEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "event_type", "actor", "note")
    readonly_fields = fields
    ordering = ("-created_at",)
    show_change_link = True


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """The order desk: look things up, move them along, never edit history."""

    list_display = (
        "number",
        "customer",
        "status",
        "total_display",
        "payment_state",
        "created_at",
    )
    list_filter = ("status", "currency", ("created_at", admin.DateFieldListFilter))
    search_fields = ("number", "user__email", "user__first_name", "user__last_name")
    list_select_related = ("user",)
    date_hierarchy = "created_at"
    inlines = (OrderItemInline, OrderAddressInline, OrderEventInline)
    readonly_fields = (
        "number",
        "user",
        "checkout",
        "status",
        "currency",
        "subtotal",
        "shipping_amount",
        "discount_amount",
        "tax_amount",
        "total",
        "shipping_code",
        "shipping_name",
        "shipping_estimate",
        "paid_at",
        "cancelled_at",
        "shipped_at",
        "delivered_at",
        "created_at",
        "updated_at",
    )
    actions = ("mark_processing", "mark_shipped", "mark_delivered", "cancel_unpaid")

    @admin.display(description=_("Customer"))
    def customer(self, obj: Order) -> str:
        return obj.user.email

    @admin.display(description=_("Total"))
    def total_display(self, obj: Order) -> str:
        return f"{obj.total} {obj.currency}"

    @admin.display(description=_("Payment"))
    def payment_state(self, obj: Order) -> str:
        payment = getattr(obj, "payment", None)
        return payment.get_status_display() if payment else "—"

    # ---------------------------------------------------------------- actions

    def _run(self, request, queryset, handler, verb: str) -> None:
        done = 0
        for order in queryset:
            try:
                handler(order, actor=request.user)
            except InvalidTransition as err:
                messages.error(request, f"{order.number}: {err}")
            else:
                done += 1
        if done:
            messages.success(
                request, _("%(count)d order(s) %(verb)s.") % {"count": done, "verb": verb}
            )

    @admin.action(description=_("Mark processing and open a shipment"))
    def mark_processing(self, request, queryset):
        self._run(request, queryset, services.mark_processing, _("marked processing"))

    @admin.action(description=_("Mark shipped"))
    def mark_shipped(self, request, queryset):
        self._run(request, queryset, services.ship_order, _("marked shipped"))

    @admin.action(description=_("Mark delivered"))
    def mark_delivered(self, request, queryset):
        self._run(request, queryset, services.deliver_order, _("marked delivered"))

    @admin.action(description=_("Cancel unpaid orders"))
    def cancel_unpaid(self, request, queryset):
        self._run(
            request,
            queryset,
            lambda order, actor: services.cancel_order(
                order, actor=actor, note="Cancelled by staff."
            ),
            _("cancelled"),
        )


class ShipmentEventInline(admin.TabularInline):
    model = ShipmentEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "event_type", "actor", "note")
    readonly_fields = fields
    ordering = ("-created_at",)


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    """Fulfilment screen: carrier details editable, status moved by action."""

    list_display = ("order", "status", "method", "carrier", "tracking_number", "estimated_delivery")
    list_filter = ("status", "method")
    search_fields = ("order__number", "tracking_number", "carrier")
    list_select_related = ("order",)
    inlines = (ShipmentEventInline,)
    readonly_fields = (
        "order",
        "status",
        "method",
        "shipped_at",
        "delivered_at",
        "created_at",
        "updated_at",
    )
    fields = (*readonly_fields, "carrier", "tracking_number", "estimated_delivery")
    actions = ("mark_shipped", "mark_delivered", "cancel_shipment")

    def _run(self, request, queryset, target: str) -> None:
        done = 0
        for shipment in queryset:
            try:
                shipment.transition_to(
                    target, actor=request.user, note=f"Marked {target} in the admin."
                )
            except InvalidTransition as err:
                messages.error(request, str(err))
            else:
                done += 1
        if done:
            messages.success(
                request,
                _("%(count)d shipment(s) marked %(target)s.") % {"count": done, "target": target},
            )

    @admin.action(description=_("Mark shipped"))
    def mark_shipped(self, request, queryset):
        self._run(request, queryset, Shipment.Status.SHIPPED)

    @admin.action(description=_("Mark delivered"))
    def mark_delivered(self, request, queryset):
        self._run(request, queryset, Shipment.Status.DELIVERED)

    @admin.action(description=_("Cancel shipment"))
    def cancel_shipment(self, request, queryset):
        self._run(request, queryset, Shipment.Status.CANCELLED)
