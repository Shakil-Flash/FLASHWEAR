"""Phase 45: Supplier Management & Purchase Order Intelligence models.

Models:
* Supplier -- vendor business credentials, contacts, currency, lead time, and active status.
* SupplierVariant -- supplier-variant sourcing matrix, purchase costs, MOQ, lead times.
* PurchaseOrder -- purchase order lifecycle (draft -> pending_approval -> approved -> ordered -> partially_received -> fully_received -> cancelled).
* PurchaseOrderItem -- order line items with frozen agreed unit purchase costs and quantities.
* PurchaseOrderReceipt -- auditable goods receipts tracking deliveries, carrier references, and receiving staff.
* PurchaseOrderReceiptItem -- received and inspected quantities (accepted sellable vs quarantined/damaged).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from apps.catalog.models import ProductVariant

__all__ = [
    "CreditNoteApplication",
    "CreditNoteStatus",
    "ExpenseBudget",
    "ExpenseCategory",
    "ExpenseStatus",
    "InvoiceMatchingStatus",
    "InvoiceMatchRecord",
    "OperatingExpense",
    "PaymentMethod",
    "PurchaseOrder",
    "PurchaseOrderItem",
    "PurchaseOrderReceipt",
    "PurchaseOrderReceiptItem",
    "PurchaseOrderStatus",
    "Supplier",
    "SupplierCreditNote",
    "SupplierInvoice",
    "SupplierInvoiceLine",
    "SupplierInvoiceStatus",
    "SupplierPayment",
    "SupplierPaymentAllocation",
    "SupplierPaymentStatus",
    "SupplierVariant",
]


class PurchaseOrderStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    PENDING_APPROVAL = "pending_approval", _("Pending approval")
    APPROVED = "approved", _("Approved")
    ORDERED = "ordered", _("Sent to supplier")
    PARTIALLY_RECEIVED = "partially_received", _("Partially received")
    FULLY_RECEIVED = "fully_received", _("Fully received")
    CANCELLED = "cancelled", _("Cancelled")


class Supplier(models.Model):
    """Vendor / Supplier business profile."""

    name = models.CharField(_("supplier name"), max_length=200, unique=True)
    code = models.CharField(
        _("supplier code"),
        max_length=64,
        unique=True,
        db_index=True,
        help_text=_("Unique business or vendor reference (e.g. SUP-001)"),
    )
    contact_name = models.CharField(_("contact person"), max_length=150, blank=True)
    email = models.EmailField(_("email address"), blank=True)
    phone = models.CharField(_("telephone"), max_length=50, blank=True)
    address_line1 = models.CharField(_("address line 1"), max_length=255, blank=True)
    address_line2 = models.CharField(_("address line 2"), max_length=255, blank=True)
    city = models.CharField(_("city"), max_length=100, blank=True)
    postal_code = models.CharField(_("postal code"), max_length=32, blank=True)
    country = models.CharField(_("country"), max_length=100, blank=True, default="United Kingdom")
    currency = models.CharField(_("currency"), max_length=3, default="USD")
    lead_time_days = models.PositiveIntegerField(
        _("expected lead time (days)"),
        null=True,
        blank=True,
        help_text=_("Average days from order placement to warehouse receipt."),
    )
    minimum_order_quantity = models.PositiveIntegerField(
        _("minimum order quantity"),
        default=1,
        help_text=_("Minimum total units required per purchase order."),
    )
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)
    notes = models.TextField(_("internal notes"), blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("supplier")
        verbose_name_plural = _("suppliers")
        ordering = ("name",)

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"

    @property
    def orders_count(self) -> int:
        return self.purchase_orders.count()

    @property
    def active_orders_count(self) -> int:
        return self.purchase_orders.exclude(
            status__in=[PurchaseOrderStatus.FULLY_RECEIVED, PurchaseOrderStatus.CANCELLED]
        ).count()

    @property
    def total_spend(self) -> Decimal:
        """Total agreed spend on approved/sent/received purchase orders."""
        orders = self.purchase_orders.filter(
            status__in=[
                PurchaseOrderStatus.APPROVED,
                PurchaseOrderStatus.ORDERED,
                PurchaseOrderStatus.PARTIALLY_RECEIVED,
                PurchaseOrderStatus.FULLY_RECEIVED,
            ]
        )
        total = Decimal("0.00")
        for order in orders:
            total += order.total_cost
        return total


class SupplierVariant(models.Model):
    """Supplier offering for a specific product variant with agreed purchase cost."""

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.CASCADE,
        related_name="supplier_variants",
        verbose_name=_("supplier"),
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.CASCADE,
        related_name="supplier_variants",
        verbose_name=_("variant"),
    )
    supplier_sku = models.CharField(
        _("supplier SKU / reference"),
        max_length=100,
        blank=True,
        help_text=_("Vendor's own product or catalogue code."),
    )
    purchase_cost = models.DecimalField(
        _("agreed purchase cost"),
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text=_("Unit acquisition cost from this supplier."),
    )
    currency = models.CharField(_("currency"), max_length=3, default="USD")
    minimum_order_quantity = models.PositiveIntegerField(
        _("minimum order quantity"),
        default=1,
        validators=[MinValueValidator(1)],
    )
    lead_time_days = models.PositiveIntegerField(
        _("lead time (days)"),
        null=True,
        blank=True,
    )
    is_preferred = models.BooleanField(_("is preferred supplier"), default=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("supplier variant")
        verbose_name_plural = _("supplier variants")
        ordering = ("supplier", "variant__sku")
        constraints = [
            models.UniqueConstraint(
                fields=["supplier", "variant"],
                name="procurement_supplier_variant_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.supplier.name} - {self.variant.sku} ({self.currency} {self.purchase_cost})"


class PurchaseOrder(models.Model):
    """Authoritative procurement order with validated lifecycle transitions."""

    reference = models.CharField(
        _("order reference"),
        max_length=64,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier, e.g. PO-20261013-0001."),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="purchase_orders",
        verbose_name=_("supplier"),
    )
    status = models.CharField(
        _("status"),
        max_length=32,
        choices=PurchaseOrderStatus.choices,
        default=PurchaseOrderStatus.DRAFT,
        db_index=True,
    )
    currency = models.CharField(_("currency"), max_length=3, default="USD")
    expected_delivery_date = models.DateField(
        _("expected delivery date"),
        null=True,
        blank=True,
        db_index=True,
    )
    notes = models.TextField(_("internal notes"), blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_purchase_orders",
        verbose_name=_("created by"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_purchase_orders",
        verbose_name=_("approved by"),
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_purchase_orders",
        verbose_name=_("cancelled by"),
    )
    cancelled_at = models.DateTimeField(_("cancelled at"), null=True, blank=True)
    cancellation_reason = models.CharField(_("cancellation reason"), max_length=255, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("purchase order")
        verbose_name_plural = _("purchase orders")
        ordering = ("-created_at", "-pk")

    def __str__(self) -> str:
        return f"{self.reference} ({self.supplier.name}) - {self.get_status_display()}"

    @property
    def total_cost(self) -> Decimal:
        """Sum of all line totals using exact Decimal arithmetic."""
        return sum((item.line_total for item in self.items.all()), Decimal("0.00"))

    @property
    def total_ordered_units(self) -> int:
        return sum((item.ordered_quantity for item in self.items.all()), 0)

    @property
    def total_received_units(self) -> int:
        return sum((item.received_quantity for item in self.items.all()), 0)

    @property
    def total_outstanding_units(self) -> int:
        return max(0, self.total_ordered_units - self.total_received_units)

    @property
    def is_overdue(self) -> bool:
        if not self.expected_delivery_date:
            return False
        return (
            self.expected_delivery_date < timezone.localdate()
            and self.status
            in (
                PurchaseOrderStatus.APPROVED,
                PurchaseOrderStatus.ORDERED,
                PurchaseOrderStatus.PARTIALLY_RECEIVED,
            )
        )

    @property
    def can_edit_items(self) -> bool:
        return self.status in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.PENDING_APPROVAL)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            PurchaseOrderStatus.FULLY_RECEIVED,
            PurchaseOrderStatus.CANCELLED,
        )

    @property
    def can_receive(self) -> bool:
        return self.status in (
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.ORDERED,
            PurchaseOrderStatus.PARTIALLY_RECEIVED,
        )


class PurchaseOrderItem(models.Model):
    """Line item in a purchase order with frozen agreed cost."""

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("purchase order"),
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="purchase_order_items",
        verbose_name=_("variant"),
    )
    ordered_quantity = models.PositiveIntegerField(
        _("ordered quantity"),
        validators=[MinValueValidator(1)],
    )
    received_quantity = models.PositiveIntegerField(
        _("received quantity"),
        default=0,
    )
    unit_cost = models.DecimalField(
        _("unit purchase cost"),
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text=_("Agreed purchase price frozen at time of ordering."),
    )
    notes = models.CharField(_("notes"), max_length=255, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("purchase order item")
        verbose_name_plural = _("purchase order items")
        ordering = ("variant__sku",)
        constraints = [
            models.UniqueConstraint(
                fields=["purchase_order", "variant"],
                name="procurement_po_item_unique",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.variant.sku} x{self.ordered_quantity} @ {self.unit_cost} "
            f"({self.received_quantity} received)"
        )

    @property
    def line_total(self) -> Decimal:
        return self.unit_cost * Decimal(self.ordered_quantity)

    @property
    def outstanding_quantity(self) -> int:
        return max(0, self.ordered_quantity - self.received_quantity)

    @property
    def is_fully_received(self) -> bool:
        return self.received_quantity >= self.ordered_quantity


class PurchaseOrderReceipt(models.Model):
    """Audit record of a physical goods receipt shipment."""

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.PROTECT,
        related_name="receipts",
        verbose_name=_("purchase order"),
    )
    delivery_reference = models.CharField(
        _("delivery reference"),
        max_length=100,
        db_index=True,
        help_text=_("Carrier tracking or supplier delivery note number."),
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_po_receipts",
        verbose_name=_("received by"),
    )
    received_at = models.DateTimeField(_("received at"), default=timezone.now, db_index=True)
    notes = models.TextField(_("receiving notes"), blank=True)
    idempotency_key = models.CharField(
        _("idempotency key"),
        max_length=64,
        blank=True,
        db_index=True,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("goods receipt")
        verbose_name_plural = _("goods receipts")
        ordering = ("-received_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=["purchase_order", "delivery_reference"],
                name="procurement_po_receipt_delivery_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"Receipt {self.delivery_reference} for {self.purchase_order.reference}"


class PurchaseOrderReceiptItem(models.Model):
    """Line item within a goods receipt with accepted vs quarantined breakdown."""

    receipt = models.ForeignKey(
        PurchaseOrderReceipt,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("receipt"),
    )
    line_item = models.ForeignKey(
        PurchaseOrderItem,
        on_delete=models.PROTECT,
        related_name="receipt_items",
        verbose_name=_("purchase order line item"),
    )
    quantity_received = models.PositiveIntegerField(
        _("quantity accepted (sellable)"),
        default=0,
        help_text=_("Units accepted and credited into warehouse sellable inventory."),
    )
    quantity_damaged = models.PositiveIntegerField(
        _("quantity damaged / quarantined"),
        default=0,
        help_text=_("Units rejected/damaged. Kept out of sellable inventory."),
    )
    notes = models.CharField(_("inspection notes"), max_length=255, blank=True)

    class Meta:
        verbose_name = _("receipt item")
        verbose_name_plural = _("receipt items")

    def __str__(self) -> str:
        return (
            f"{self.line_item.variant.sku}: accepted {self.quantity_received}, "
            f"damaged {self.quantity_damaged}"
        )


# =============================================================================
# Phase 46: Supplier Invoices & Accounts Payable Intelligence Models
# =============================================================================


class SupplierInvoiceStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    SUBMITTED = "submitted", _("Submitted for review")
    APPROVED = "approved", _("Approved (Liability recognized)")
    PARTIALLY_PAID = "partially_paid", _("Partially paid")
    PAID = "paid", _("Paid in full")
    DISPUTED = "disputed", _("Disputed")
    VOIDED = "voided", _("Voided")


class InvoiceMatchingStatus(models.TextChoices):
    MATCHED = "matched", _("Fully matched (3-way)")
    VARIANCE_QUANTITY = "variance_quantity", _("Quantity variance")
    VARIANCE_PRICE = "variance_price", _("Price variance")
    MISSING_RECEIPT = "missing_receipt", _("Missing goods receipt")
    OVERBILLED = "overbilled", _("Overbilled against PO")
    UNMATCHED = "unmatched", _("Unmatched / manual bill")


class PaymentMethod(models.TextChoices):
    BANK_TRANSFER = "bank_transfer", _("Bank Transfer / BACS")
    WIRE = "wire", _("Wire Transfer / SWIFT")
    CORPORATE_CARD = "corporate_card", _("Corporate Card")
    CHECK = "check", _("Check / Cheque")
    ACH = "ach", _("ACH / Direct Debit")
    OTHER = "other", _("Other")


class SupplierPaymentStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    COMPLETED = "completed", _("Completed")
    FAILED = "failed", _("Failed")
    REVERSED = "reversed", _("Reversed / Voided")


class CreditNoteStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    APPROVED = "approved", _("Approved")
    APPLIED = "applied", _("Applied in full")
    VOIDED = "voided", _("Voided")


class SupplierInvoice(models.Model):
    """Supplier invoice / vendor bill tracking liabilities, 3-way matching, and settlement."""

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="invoices",
        verbose_name=_("supplier"),
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices",
        verbose_name=_("linked purchase order"),
    )
    invoice_number = models.CharField(
        _("invoice number"),
        max_length=100,
        help_text=_("Supplier invoice or bill number as stated on document."),
    )
    normalized_invoice_number = models.CharField(
        _("normalized invoice number"),
        max_length=100,
        db_index=True,
        help_text=_("Standardized uppercase alphanumeric token for duplicate prevention."),
    )
    supplier_reference = models.CharField(
        _("supplier reference"),
        max_length=100,
        blank=True,
        help_text=_("Vendor internal reference or account code."),
    )
    status = models.CharField(
        _("status"),
        max_length=32,
        choices=SupplierInvoiceStatus.choices,
        default=SupplierInvoiceStatus.DRAFT,
        db_index=True,
    )
    invoice_date = models.DateField(
        _("invoice date"),
        default=timezone.localdate,
        db_index=True,
    )
    received_date = models.DateField(
        _("received date"),
        default=timezone.localdate,
    )
    due_date = models.DateField(
        _("due date"),
        db_index=True,
    )
    currency = models.CharField(
        _("currency"),
        max_length=3,
        default="USD",
    )
    subtotal = models.DecimalField(
        _("subtotal"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    tax_amount = models.DecimalField(
        _("tax amount"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    shipping_amount = models.DecimalField(
        _("shipping amount"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    discount_amount = models.DecimalField(
        _("discount amount"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    total_amount = models.DecimalField(
        _("total amount"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    amount_paid = models.DecimalField(
        _("amount paid"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    amount_credited = models.DecimalField(
        _("amount credited"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    is_duplicate_override = models.BooleanField(
        _("is duplicate override"),
        default=False,
        help_text=_("Authorized exception allowing an identical invoice identifier."),
    )
    duplicate_override_reason = models.CharField(
        _("duplicate override reason"),
        max_length=255,
        blank=True,
    )
    attachment = models.FileField(
        _("supporting document"),
        upload_to="procurement/invoices/%Y/%m",
        blank=True,
        null=True,
        help_text=_("Scanned bill, PDF or supporting delivery documentation."),
    )
    internal_notes = models.TextField(
        _("internal notes"),
        blank=True,
    )
    dispute_reason = models.CharField(
        _("dispute reason"),
        max_length=255,
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_supplier_invoices",
        verbose_name=_("prepared by"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_supplier_invoices",
        verbose_name=_("approved by"),
    )
    approved_at = models.DateTimeField(
        _("approved at"),
        null=True,
        blank=True,
    )
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voided_supplier_invoices",
        verbose_name=_("voided by"),
    )
    voided_at = models.DateTimeField(
        _("voided at"),
        null=True,
        blank=True,
    )
    void_reason = models.CharField(
        _("void reason"),
        max_length=255,
        blank=True,
    )
    created_at = models.DateTimeField(
        _("created at"),
        auto_now_add=True,
        db_index=True,
    )
    updated_at = models.DateTimeField(
        _("updated at"),
        auto_now=True,
    )

    class Meta:
        verbose_name = _("supplier invoice")
        verbose_name_plural = _("supplier invoices")
        ordering = ("-invoice_date", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=["supplier", "normalized_invoice_number"],
                condition=~models.Q(status="voided") & models.Q(is_duplicate_override=False),
                name="procurement_supplier_invoice_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(total_amount__gte=models.F("amount_paid") + models.F("amount_credited")),
                name="procurement_invoice_total_gte_settled",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_paid__gte=0),
                name="procurement_invoice_paid_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_credited__gte=0),
                name="procurement_invoice_credited_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(subtotal__gte=0),
                name="procurement_invoice_subtotal_gte_0",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.invoice_number} ({self.supplier.name}) - {self.get_status_display()}"

    @property
    def subtotal_amount(self) -> Decimal:
        """Alias for subtotal."""
        return self.subtotal

    @property
    def outstanding_balance(self) -> Decimal:
        """Remaining unpaid balance after deducting payments and credit applications."""
        return max(Decimal("0.00"), self.total_amount - self.amount_paid - self.amount_credited)

    @property
    def is_overdue(self) -> bool:
        """Invoice is past its due date with an outstanding recognized balance."""
        today = timezone.localdate()
        return (
            self.due_date < today
            and self.status in (SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID)
            and self.outstanding_balance > Decimal("0.00")
        )

    @property
    def days_overdue(self) -> int:
        today = timezone.localdate()
        if self.due_date < today and self.outstanding_balance > Decimal("0.00"):
            return (today - self.due_date).days
        return 0

    @property
    def can_edit(self) -> bool:
        return self.status == SupplierInvoiceStatus.DRAFT

    @property
    def can_approve(self) -> bool:
        return self.status in (SupplierInvoiceStatus.SUBMITTED, SupplierInvoiceStatus.DISPUTED)

    @property
    def can_pay(self) -> bool:
        return (
            self.status in (SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID)
            and self.outstanding_balance > Decimal("0.00")
        )

    @property
    def can_void(self) -> bool:
        return (
            self.status in (SupplierInvoiceStatus.DRAFT, SupplierInvoiceStatus.SUBMITTED, SupplierInvoiceStatus.DISPUTED, SupplierInvoiceStatus.APPROVED)
            and self.amount_paid == Decimal("0.00")
            and self.amount_credited == Decimal("0.00")
        )


class SupplierInvoiceLine(models.Model):
    """Line item on a supplier invoice linked optionally to a PO line and variant."""

    invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("invoice"),
    )
    po_item = models.ForeignKey(
        PurchaseOrderItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoice_lines",
        verbose_name=_("linked purchase order item"),
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoice_lines",
        verbose_name=_("variant"),
    )
    description = models.CharField(
        _("description"),
        max_length=255,
    )
    quantity = models.PositiveIntegerField(
        _("quantity"),
        validators=[MinValueValidator(1)],
    )
    unit_cost = models.DecimalField(
        _("unit cost"),
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    line_total = models.DecimalField(
        _("line total"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    notes = models.CharField(
        _("notes"),
        max_length=255,
        blank=True,
    )
    created_at = models.DateTimeField(
        _("created at"),
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        _("updated at"),
        auto_now=True,
    )

    class Meta:
        verbose_name = _("invoice line item")
        verbose_name_plural = _("invoice line items")
        ordering = ("pk",)

    def __str__(self) -> str:
        return f"{self.description} x{self.quantity} @ {self.unit_cost} = {self.line_total}"


class InvoiceMatchRecord(models.Model):
    """Three-way match analysis comparing purchase order, warehouse receipts, and bill."""

    invoice = models.OneToOneField(
        SupplierInvoice,
        on_delete=models.CASCADE,
        related_name="match_record",
        verbose_name=_("invoice"),
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="match_records",
        verbose_name=_("purchase order"),
    )
    status = models.CharField(
        _("matching status"),
        max_length=32,
        choices=InvoiceMatchingStatus.choices,
        default=InvoiceMatchingStatus.UNMATCHED,
        db_index=True,
    )
    price_variance_total = models.DecimalField(
        _("price variance total"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=_("Total net monetary variance between invoice unit costs and agreed PO costs."),
    )
    quantity_variance_total = models.IntegerField(
        _("quantity variance total"),
        default=0,
        help_text=_("Total unit variance between invoiced quantity and accepted received quantity."),
    )
    is_within_tolerance = models.BooleanField(
        _("is within tolerance"),
        default=False,
    )
    summary = models.TextField(
        _("match summary"),
        blank=True,
    )
    discrepancy_details = models.JSONField(
        _("discrepancy details"),
        default=list,
        blank=True,
    )
    performed_at = models.DateTimeField(
        _("performed at"),
        default=timezone.now,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_invoice_matches",
        verbose_name=_("reviewed by"),
    )
    reviewed_at = models.DateTimeField(
        _("reviewed at"),
        null=True,
        blank=True,
    )
    override_approved = models.BooleanField(
        _("override approved"),
        default=False,
        help_text=_("Explicit staff override of match discrepancies."),
    )
    override_reason = models.CharField(
        _("override reason"),
        max_length=255,
        blank=True,
    )

    class Meta:
        verbose_name = _("invoice match record")
        verbose_name_plural = _("invoice match records")
        ordering = ("-performed_at",)

    def __str__(self) -> str:
        return f"Match Record for {self.invoice.invoice_number} ({self.get_status_display()})"

    @property
    def discrepancy_override_by(self) -> Any:
        return self.reviewed_by

    @property
    def discrepancies(self) -> list[dict[str, Any]]:
        return self.discrepancy_details or []


class SupplierPayment(models.Model):
    """Internal disbursement record settling one or more approved supplier invoices."""

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="payments",
        verbose_name=_("supplier"),
    )
    payment_reference = models.CharField(
        _("payment reference"),
        max_length=64,
        unique=True,
        db_index=True,
        help_text=_("Unique internal disbursement reference, e.g. PAY-AP-20261013-0001."),
    )
    bank_transaction_reference = models.CharField(
        _("bank / external transaction ref"),
        max_length=100,
        blank=True,
        db_index=True,
        help_text=_("Bank statement, wire transfer or payment reference number."),
    )
    payment_method = models.CharField(
        _("payment method"),
        max_length=32,
        choices=PaymentMethod.choices,
        default=PaymentMethod.BANK_TRANSFER,
    )
    payment_date = models.DateField(
        _("payment date"),
        default=timezone.localdate,
        db_index=True,
    )
    amount = models.DecimalField(
        _("payment amount"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    currency = models.CharField(
        _("currency"),
        max_length=3,
        default="USD",
    )
    status = models.CharField(
        _("status"),
        max_length=32,
        choices=SupplierPaymentStatus.choices,
        default=SupplierPaymentStatus.COMPLETED,
        db_index=True,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_supplier_payments",
        verbose_name=_("recorded by"),
    )
    notes = models.TextField(
        _("internal notes"),
        blank=True,
    )
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reversed_supplier_payments",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(
        _("reversed at"),
        null=True,
        blank=True,
    )
    reversal_reason = models.CharField(
        _("reversal reason"),
        max_length=255,
        blank=True,
    )
    created_at = models.DateTimeField(
        _("created at"),
        auto_now_add=True,
        db_index=True,
    )
    updated_at = models.DateTimeField(
        _("updated at"),
        auto_now=True,
    )

    class Meta:
        verbose_name = _("supplier payment")
        verbose_name_plural = _("supplier payments")
        ordering = ("-payment_date", "-pk")

    def __str__(self) -> str:
        return f"{self.payment_reference} ({self.supplier.name}) - {self.currency} {self.amount}"

    @property
    def allocated_amount(self) -> Decimal:
        return sum((alloc.amount for alloc in self.allocations.all()), Decimal("0.00"))

    @property
    def unallocated_amount(self) -> Decimal:
        return max(Decimal("0.00"), self.amount - self.allocated_amount)

    @property
    def can_reverse(self) -> bool:
        return self.status == SupplierPaymentStatus.COMPLETED


class SupplierPaymentAllocation(models.Model):
    """Portion of a supplier payment applied toward a specific approved invoice."""

    payment = models.ForeignKey(
        SupplierPayment,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name=_("payment"),
    )
    invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.PROTECT,
        related_name="payment_allocations",
        verbose_name=_("invoice"),
    )
    amount = models.DecimalField(
        _("allocated amount"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    created_at = models.DateTimeField(
        _("allocated at"),
        auto_now_add=True,
    )

    class Meta:
        verbose_name = _("payment allocation")
        verbose_name_plural = _("payment allocations")
        ordering = ("pk",)
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "invoice"],
                name="procurement_payment_invoice_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="procurement_allocation_amount_gt_0",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.payment.payment_reference} -> {self.invoice.invoice_number}: {self.amount}"


class SupplierCreditNote(models.Model):
    """Credit memo issued by a supplier reducing accounts payable liability."""

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="credit_notes",
        verbose_name=_("supplier"),
    )
    invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credit_notes",
        verbose_name=_("original invoice"),
    )
    credit_note_number = models.CharField(
        _("credit note number"),
        max_length=100,
        db_index=True,
    )
    credit_date = models.DateField(
        _("credit date"),
        default=timezone.localdate,
    )
    amount = models.DecimalField(
        _("credit amount"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    amount_applied = models.DecimalField(
        _("amount applied"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    currency = models.CharField(
        _("currency"),
        max_length=3,
        default="USD",
    )
    status = models.CharField(
        _("status"),
        max_length=32,
        choices=CreditNoteStatus.choices,
        default=CreditNoteStatus.APPROVED,
        db_index=True,
    )
    reason = models.CharField(
        _("reason"),
        max_length=255,
        help_text=_("e.g. Return of damaged goods, agreed price concession, discount"),
    )
    notes = models.TextField(
        _("internal notes"),
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_supplier_credit_notes",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(
        _("created at"),
        auto_now_add=True,
        db_index=True,
    )
    updated_at = models.DateTimeField(
        _("updated at"),
        auto_now=True,
    )

    class Meta:
        verbose_name = _("supplier credit note")
        verbose_name_plural = _("supplier credit notes")
        ordering = ("-credit_date", "-pk")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=models.F("amount_applied")),
                name="procurement_credit_note_amount_gte_applied",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_applied__gte=0),
                name="procurement_credit_note_applied_gte_0",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.credit_note_number} ({self.supplier.name}) - {self.currency} {self.amount}"

    @property
    def unapplied_amount(self) -> Decimal:
        return max(Decimal("0.00"), self.amount - self.amount_applied)

    @property
    def remaining_amount(self) -> Decimal:
        return self.unapplied_amount

    @property
    def can_apply(self) -> bool:
        return (
            self.status == CreditNoteStatus.APPROVED
            and self.unapplied_amount > Decimal("0.00")
        )


class CreditNoteApplication(models.Model):
    """Application of a credit note balance toward settling an approved supplier invoice."""

    credit_note = models.ForeignKey(
        SupplierCreditNote,
        on_delete=models.CASCADE,
        related_name="applications",
        verbose_name=_("credit note"),
    )
    invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.PROTECT,
        related_name="credit_applications",
        verbose_name=_("invoice"),
    )
    amount = models.DecimalField(
        _("applied amount"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    applied_at = models.DateTimeField(
        _("applied at"),
        auto_now_add=True,
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_credit_notes",
        verbose_name=_("applied by"),
    )

    class Meta:
        verbose_name = _("credit note application")
        verbose_name_plural = _("credit note applications")
        ordering = ("pk",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="procurement_credit_application_amount_gt_0",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.credit_note.credit_note_number} -> {self.invoice.invoice_number}: {self.amount}"


# =============================================================================
# Phase 47: Operating Expense Management & Budgets
# =============================================================================


class ExpenseStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    SUBMITTED = "submitted", _("Submitted for Approval")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")
    PAID = "paid", _("Paid")
    VOIDED = "voided", _("Voided")


class ExpenseCategory(models.Model):
    """Operating expense category for cost accounting and departmental budgeting."""

    name = models.CharField(_("category name"), max_length=100, unique=True)
    code = models.CharField(_("category code"), max_length=32, unique=True, db_index=True)
    description = models.TextField(_("description"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("expense category")
        verbose_name_plural = _("expense categories")
        ordering = ("name",)

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class ExpenseBudget(models.Model):
    """Monthly planned spending limit for an expense category."""

    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.CASCADE,
        related_name="budgets",
        verbose_name=_("category"),
    )
    year = models.PositiveIntegerField(_("year"))
    month = models.PositiveSmallIntegerField(
        _("month"),
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    budget_amount = models.DecimalField(
        _("budget amount"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    currency = models.CharField(_("currency"), max_length=3, default="USD")
    warning_threshold_percent = models.PositiveSmallIntegerField(
        _("warning threshold %"),
        default=80,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text=_("Trigger a budget warning when actual spending reaches this % of budget."),
    )
    notes = models.TextField(_("notes"), blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_budgets",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("expense budget")
        verbose_name_plural = _("expense budgets")
        ordering = ("-year", "-month", "category__name")
        constraints = [
            models.UniqueConstraint(
                fields=["category", "year", "month"],
                name="procurement_budget_unique_category_year_month",
            ),
            models.CheckConstraint(
                condition=models.Q(budget_amount__gte=0),
                name="procurement_budget_amount_gte_0",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category.name} ({self.period}): {self.currency} {self.budget_amount}"

    @property
    def period(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


class OperatingExpense(models.Model):
    """General operational overhead expenditure (OpEx)."""

    expense_number = models.CharField(
        _("expense number"),
        max_length=32,
        unique=True,
        db_index=True,
        help_text=_("Internal handle: EXP-YYYYMM-XXXX."),
    )
    title = models.CharField(_("title"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="expenses",
        verbose_name=_("category"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operating_expenses",
        verbose_name=_("supplier / vendor"),
    )
    payee_name = models.CharField(
        _("payee name"),
        max_length=200,
        blank=True,
        help_text=_("Vendor, contractor, landlord, or payee if not an existing supplier."),
    )
    supplier_invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="linked_operating_expenses",
        verbose_name=_("originating supplier invoice"),
        help_text=_("Link to supplier invoice if expense originates from a vendor bill to prevent duplicate records."),
    )
    expense_date = models.DateField(
        _("expense date"),
        default=timezone.localdate,
        db_index=True,
    )
    accounting_period = models.CharField(
        _("accounting period"),
        max_length=7,
        db_index=True,
        help_text=_("Reporting month: YYYY-MM."),
    )
    amount = models.DecimalField(
        _("pre-tax amount"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    tax_amount = models.DecimalField(
        _("tax amount"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    total_amount = models.DecimalField(
        _("total amount"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    currency = models.CharField(_("currency"), max_length=3, default="USD")
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=ExpenseStatus.choices,
        default=ExpenseStatus.DRAFT,
        db_index=True,
    )
    payment_date = models.DateField(_("payment date"), null=True, blank=True)
    payment_method = models.CharField(
        _("payment method"),
        max_length=32,
        choices=PaymentMethod.choices,
        blank=True,
    )
    payment_reference = models.CharField(
        _("payment reference"),
        max_length=100,
        blank=True,
        help_text=_("Bank transfer ID, card transaction reference, or check number."),
    )
    is_recurring = models.BooleanField(_("is recurring"), default=False)
    recurrence_frequency = models.CharField(
        _("recurrence frequency"),
        max_length=20,
        blank=True,
        choices=[
            ("weekly", _("Weekly")),
            ("monthly", _("Monthly")),
            ("quarterly", _("Quarterly")),
            ("annually", _("Annually")),
        ],
    )
    receipt = models.FileField(
        _("receipt / supporting document"),
        upload_to="expenses/receipts/%Y/%m/",
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_expenses",
        verbose_name=_("created by"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_expenses",
        verbose_name=_("approved by"),
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    rejection_reason = models.TextField(_("rejection reason"), blank=True)
    notes = models.TextField(_("notes"), blank=True)
    correction_reason = models.TextField(
        _("correction reason"),
        blank=True,
        help_text=_("Documented rationale for audited modifications after creation."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("operating expense")
        verbose_name_plural = _("operating expenses")
        ordering = ("-expense_date", "-pk")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0),
                name="procurement_expense_amount_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(tax_amount__gte=0),
                name="procurement_expense_tax_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(total_amount__gte=0),
                name="procurement_expense_total_gte_0",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.expense_number} - {self.title} ({self.currency} {self.total_amount})"

    @property
    def can_edit(self) -> bool:
        return self.status in (ExpenseStatus.DRAFT, ExpenseStatus.REJECTED)

    @property
    def can_submit(self) -> bool:
        return self.status == ExpenseStatus.DRAFT

    @property
    def can_approve(self) -> bool:
        return self.status == ExpenseStatus.SUBMITTED

    @property
    def can_pay(self) -> bool:
        return self.status == ExpenseStatus.APPROVED

    @property
    def can_void(self) -> bool:
        return self.status in (ExpenseStatus.DRAFT, ExpenseStatus.SUBMITTED, ExpenseStatus.APPROVED)

