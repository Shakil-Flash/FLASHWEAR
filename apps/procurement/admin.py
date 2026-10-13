"""Django admin registrations for apps.procurement."""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.procurement.models import (
    CreditNoteApplication,
    ExpenseBudget,
    ExpenseCategory,
    InvoiceMatchRecord,
    OperatingExpense,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptItem,
    Supplier,
    SupplierCreditNote,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierPayment,
    SupplierPaymentAllocation,
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


class SupplierInvoiceLineInline(admin.TabularInline):
    model = SupplierInvoiceLine
    extra = 0
    raw_id_fields = ("po_item",)
    autocomplete_fields = ("variant",)


class InvoiceMatchRecordInline(admin.StackedInline):
    model = InvoiceMatchRecord
    extra = 0
    readonly_fields = ("status", "price_variance_total", "quantity_variance_total", "is_within_tolerance", "summary", "performed_at")


@admin.register(SupplierInvoice)
class SupplierInvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "supplier",
        "status",
        "invoice_date",
        "due_date",
        "total_amount",
        "amount_paid",
        "amount_credited",
        "currency",
    )
    list_filter = ("status", "currency", "invoice_date", "due_date")
    search_fields = ("invoice_number", "normalized_invoice_number", "supplier__name", "supplier_reference")
    autocomplete_fields = ("supplier", "purchase_order")
    readonly_fields = ("created_at", "updated_at", "approved_at", "voided_at")
    inlines = (SupplierInvoiceLineInline, InvoiceMatchRecordInline)


class SupplierPaymentAllocationInline(admin.TabularInline):
    model = SupplierPaymentAllocation
    extra = 0
    autocomplete_fields = ("invoice",)


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "payment_reference",
        "supplier",
        "amount",
        "currency",
        "payment_method",
        "status",
        "payment_date",
    )
    list_filter = ("status", "payment_method", "currency", "payment_date")
    search_fields = ("payment_reference", "bank_transaction_reference", "supplier__name")
    autocomplete_fields = ("supplier",)
    readonly_fields = ("created_at", "updated_at", "reversed_at")
    inlines = (SupplierPaymentAllocationInline,)


class CreditNoteApplicationInline(admin.TabularInline):
    model = CreditNoteApplication
    extra = 0
    autocomplete_fields = ("invoice",)


@admin.register(SupplierCreditNote)
class SupplierCreditNoteAdmin(admin.ModelAdmin):
    list_display = (
        "credit_note_number",
        "supplier",
        "amount",
        "amount_applied",
        "currency",
        "status",
        "credit_date",
    )
    list_filter = ("status", "currency", "credit_date")
    search_fields = ("credit_note_number", "supplier__name", "reason")
    autocomplete_fields = ("supplier", "invoice")
    readonly_fields = ("created_at", "updated_at")
    inlines = (CreditNoteApplicationInline,)


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "code", "description")


@admin.register(ExpenseBudget)
class ExpenseBudgetAdmin(admin.ModelAdmin):
    list_display = (
        "category",
        "year",
        "month",
        "budget_amount",
        "currency",
        "warning_threshold_percent",
    )
    list_filter = ("year", "month", "category")
    search_fields = ("category__name", "category__code", "notes")
    autocomplete_fields = ("category",)


@admin.register(OperatingExpense)
class OperatingExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "expense_number",
        "title",
        "category",
        "supplier",
        "payee_name",
        "total_amount",
        "currency",
        "status",
        "expense_date",
        "payment_date",
    )
    list_filter = ("status", "category", "currency", "expense_date", "is_recurring")
    search_fields = ("expense_number", "title", "payee_name", "supplier__name", "payment_reference")
    autocomplete_fields = ("category", "supplier", "supplier_invoice")
    readonly_fields = ("created_at", "updated_at", "approved_at")

