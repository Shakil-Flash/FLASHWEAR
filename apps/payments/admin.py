"""Payments admin: read-only payment attempts, provider events, and refunds.

Card data is never stored, and payment states are modified only through services.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.payments.models import Payment, PaymentEvent, Refund

__all__ = ["PaymentAdmin", "RefundAdmin"]


class PaymentEventInline(admin.TabularInline):
    model = PaymentEvent
    extra = 0
    can_delete = False
    fields = ("received_at", "provider", "event_id", "event_type")
    readonly_fields = fields
    ordering = ("-received_at",)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Payment attempts: read-only facts and provider event timeline."""

    list_display = (
        "order",
        "provider",
        "amount_display",
        "status",
        "provider_reference",
        "paid_at",
        "created_at",
    )
    list_filter = ("status", "provider", ("created_at", admin.DateFieldListFilter))
    search_fields = ("order__number", "provider_reference")
    list_select_related = ("order",)
    date_hierarchy = "created_at"
    inlines = (PaymentEventInline,)
    readonly_fields = (
        "order",
        "provider",
        "provider_reference",
        "amount",
        "currency",
        "status",
        "failure_code",
        "failure_message",
        "paid_at",
        "created_at",
        "updated_at",
    )

    @admin.display(description=_("Amount"))
    def amount_display(self, obj: Payment) -> str:
        return f"{obj.amount} {obj.currency}"


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    """Refunds issued against payments and return requests."""

    list_display = (
        "number",
        "order",
        "amount_display",
        "status",
        "reason",
        "provider",
        "processed_at",
    )
    list_filter = ("status", "reason", "provider", ("created_at", admin.DateFieldListFilter))
    search_fields = ("number", "order__number", "provider_reference")
    list_select_related = ("order", "payment", "return_request")
    date_hierarchy = "created_at"
    readonly_fields = (
        "number",
        "order",
        "payment",
        "return_request",
        "amount",
        "currency",
        "status",
        "reason",
        "provider",
        "provider_reference",
        "is_shipping_refunded",
        "note",
        "failure_code",
        "failure_message",
        "processed_at",
        "created_at",
        "updated_at",
    )

    @admin.display(description=_("Amount"))
    def amount_display(self, obj: Refund) -> str:
        return f"{obj.amount} {obj.currency}"
