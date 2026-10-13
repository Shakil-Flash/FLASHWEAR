"""Django admin registrations for apps.procurement."""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.procurement.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptItem,
    Supplier,
    SupplierVariant,
)


class SupplierVariantInline(admin.TabularInline):
    model = SupplierVariant
    extra = 1
    autocomplete_fields = ("variant",)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "code",
        "contact_name",
        "email",
        "phone",
        "currency",
        "lead_time_days",
        "is_active",
    )
    list_filter = ("is_active", "currency", "country")
    search_fields = ("name", "code", "contact_name", "email")
    inlines = (SupplierVariantInline,)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "code",
                    "is_active",
                    "currency",
                )
            },
        ),
        (
            _("Contact & Logistics"),
            {
                "fields": (
                    "contact_name",
                    "email",
                    "phone",
                    "address_line1",
                    "address_line2",
                    "city",
                    "postal_code",
                    "country",
                    "lead_time_days",
                    "minimum_order_quantity",
                )
            },
        ),
        (
            _("Notes"),
            {
                "fields": ("notes",),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(SupplierVariant)
class SupplierVariantAdmin(admin.ModelAdmin):
    list_display = (
        "supplier",
        "variant",
        "supplier_sku",
        "purchase_cost",
        "currency",
        "minimum_order_quantity",
        "lead_time_days",
        "is_preferred",
    )
    list_filter = ("is_preferred", "currency", "supplier")
    search_fields = ("supplier__name", "variant__sku", "supplier_sku")
    autocomplete_fields = ("supplier", "variant")


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 0
    autocomplete_fields = ("variant",)
    readonly_fields = ("received_quantity",)


class PurchaseOrderReceiptInline(admin.TabularInline):
    model = PurchaseOrderReceipt
    extra = 0
    readonly_fields = ("delivery_reference", "received_by", "received_at")
    can_delete = False


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "supplier",
        "status",
        "currency",
        "expected_delivery_date",
        "created_by",
        "created_at",
    )
    list_filter = ("status", "supplier", "created_at")
    search_fields = ("reference", "supplier__name", "supplier__code")
    autocomplete_fields = ("supplier",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "approved_at",
        "sent_at",
        "cancelled_at",
    )
    inlines = (PurchaseOrderItemInline, PurchaseOrderReceiptInline)


class PurchaseOrderReceiptItemInline(admin.TabularInline):
    model = PurchaseOrderReceiptItem
    extra = 0
    readonly_fields = ("line_item", "quantity_received", "quantity_damaged", "notes")
    can_delete = False


@admin.register(PurchaseOrderReceipt)
class PurchaseOrderReceiptAdmin(admin.ModelAdmin):
    list_display = (
        "delivery_reference",
        "purchase_order",
        "received_by",
        "received_at",
    )
    search_fields = ("delivery_reference", "purchase_order__reference")
    readonly_fields = ("created_at",)
    inlines = (PurchaseOrderReceiptItemInline,)
