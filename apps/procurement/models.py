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
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from apps.catalog.models import ProductVariant

__all__ = [
    "PurchaseOrder",
    "PurchaseOrderItem",
    "PurchaseOrderReceipt",
    "PurchaseOrderReceiptItem",
    "PurchaseOrderStatus",
    "Supplier",
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
