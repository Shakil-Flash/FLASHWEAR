"""Phase 45: Procurement & Purchase Order domain services.

Authoritative operations for:
1. Supplier profile creation and maintenance.
2. Sourcing matrix (SupplierVariant) configuration.
3. Purchase Order lifecycle transitions (draft -> pending_approval -> approved -> ordered -> received / cancelled).
4. Atomic goods receiving integrated with apps.inventory.services.adjust_stock and the immutable InventoryMovement ledger.
5. Strict validations: over-receipt prevention, idempotency, Decimal precision, and audit logging.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.backoffice.models import AuditEvent
from apps.backoffice.services import audit as audit_service
from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryMovement
from apps.inventory.services import adjust_stock
from apps.procurement.models import (
    CreditNoteApplication,
    CreditNoteStatus,
    InvoiceMatchingStatus,
    InvoiceMatchRecord,
    PaymentMethod,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptItem,
    PurchaseOrderStatus,
    Supplier,
    SupplierCreditNote,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceStatus,
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentStatus,
    SupplierVariant,
)

__all__ = [
    "CreditNoteApplicationError",
    "DiscrepancyApprovalRequired",
    "DuplicateInvoiceError",
    "DuplicateReceiptError",
    "InsufficientInvoiceBalanceError",
    "InvalidInvoiceOperationError",
    "InvalidStatusTransition",
    "OrderImmutableError",
    "OverReceiptError",
    "PaymentReversalError",
    "ProcurementError",
    "SeparationOfDutiesError",
    "apply_credit_note_to_invoice",
    "approve_purchase_order",
    "approve_supplier_invoice",
    "cancel_purchase_order",
    "create_purchase_order",
    "create_supplier",
    "create_supplier_credit_note",
    "create_supplier_invoice",
    "dispute_supplier_invoice",
    "generate_payment_reference",
    "generate_po_reference",
    "normalize_invoice_number",
    "perform_three_way_match",
    "receive_purchase_order_delivery",
    "record_supplier_payment",
    "reverse_supplier_payment",
    "send_purchase_order",
    "submit_for_approval",
    "submit_supplier_invoice",
    "update_purchase_order",
    "update_supplier",
    "update_supplier_invoice",
    "void_supplier_invoice",
]


class ProcurementError(Exception):
    """Base exception for procurement domain failures."""


class InvalidStatusTransition(ProcurementError):
    """Attempted illegal lifecycle transition."""


class OrderImmutableError(ProcurementError):
    """Cannot modify an order that has progressed beyond draft/pending approval."""


class OverReceiptError(ProcurementError):
    """Received units exceed outstanding purchase order quantities."""


class DuplicateReceiptError(ProcurementError):
    """Duplicate delivery reference submitted for the same purchase order."""


class DuplicateInvoiceError(ProcurementError):
    """Duplicate supplier invoice detected."""


class DiscrepancyApprovalRequired(ProcurementError):
    """Material discrepancies block automatic approval without authorized override."""


class SeparationOfDutiesError(ProcurementError):
    """Preparer cannot approve their own invoice under governance rules."""


class InvalidInvoiceOperationError(ProcurementError):
    """Illegal operation attempted on invoice in current status."""


class InsufficientInvoiceBalanceError(ProcurementError):
    """Payment or credit allocation exceeds permitted invoice balance."""


class PaymentReversalError(ProcurementError):
    """Payment cannot be reversed in current state."""


class CreditNoteApplicationError(ProcurementError):
    """Credit note cannot be applied in current state."""


# =============================================================================
# Reference Generation
# =============================================================================


def generate_po_reference() -> str:
    """Generate a unique sequential purchase order reference.

    Format: PO-YYYYMMDD-XXXX (e.g. PO-20261013-0001).
    """
    date_str = timezone.now().strftime("%Y%m%d")
    prefix = f"PO-{date_str}-"
    # Find the highest sequence for today
    last_po = (
        PurchaseOrder.objects.filter(reference__startswith=prefix)
        .order_by("-reference")
        .values_list("reference", flat=True)
        .first()
    )
    if last_po:
        try:
            last_seq = int(last_po.split("-")[-1])
            new_seq = last_seq + 1
        except (ValueError, IndexError):
            new_seq = 1
    else:
        new_seq = 1

    reference = f"{prefix}{new_seq:04d}"
    # Guard against race collisions
    while PurchaseOrder.objects.filter(reference=reference).exists():
        new_seq += 1
        reference = f"{prefix}{new_seq:04d}"
    return reference


# =============================================================================
# Supplier Services
# =============================================================================


def create_supplier(
    *,
    name: str,
    code: str,
    contact_name: str = "",
    email: str = "",
    phone: str = "",
    address_line1: str = "",
    address_line2: str = "",
    city: str = "",
    postal_code: str = "",
    country: str = "United Kingdom",
    currency: str = "USD",
    lead_time_days: int | None = None,
    minimum_order_quantity: int = 1,
    is_active: bool = True,
    notes: str = "",
    actor=None,
) -> Supplier:
    """Create a new supplier business profile."""
    name = name.strip()
    code = code.strip().upper()
    if not name:
        raise ValidationError(_("Supplier name is required."))
    if not code:
        raise ValidationError(_("Supplier business code is required."))

    if Supplier.objects.filter(name=name).exists():
        raise ValidationError(_("A supplier with this name already exists."))
    if Supplier.objects.filter(code=code).exists():
        raise ValidationError(_("A supplier with this code already exists."))

    supplier = Supplier.objects.create(
        name=name,
        code=code,
        contact_name=contact_name.strip(),
        email=email.strip(),
        phone=phone.strip(),
        address_line1=address_line1.strip(),
        address_line2=address_line2.strip(),
        city=city.strip(),
        postal_code=postal_code.strip(),
        country=country.strip(),
        currency=currency.strip().upper() or "USD",
        lead_time_days=lead_time_days,
        minimum_order_quantity=max(1, minimum_order_quantity),
        is_active=is_active,
        notes=notes.strip(),
    )

    audit_service.record(
        actor=actor,
        domain=AuditEvent.Domain.CATALOG,
        action="supplier.create",
        object_type="procurement.supplier",
        object_id=supplier.pk,
        object_repr=str(supplier),
        reason=f"Created supplier {supplier.code}",
        metadata={"name": supplier.name, "code": supplier.code},
    )
    return supplier


def update_supplier(
    supplier: Supplier,
    *,
    name: str | None = None,
    contact_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    address_line1: str | None = None,
    address_line2: str | None = None,
    city: str | None = None,
    postal_code: str | None = None,
    country: str | None = None,
    currency: str | None = None,
    lead_time_days: int | None = None,
    minimum_order_quantity: int | None = None,
    is_active: bool | None = None,
    notes: str | None = None,
    actor=None,
) -> Supplier:
    """Update supplier business profile."""
    update_fields = ["updated_at"]

    if name is not None and name.strip():
        name_clean = name.strip()
        if (
            Supplier.objects.filter(name=name_clean)
            .exclude(pk=supplier.pk)
            .exists()
        ):
            raise ValidationError(_("A supplier with this name already exists."))
        supplier.name = name_clean
        update_fields.append("name")

    if contact_name is not None:
        supplier.contact_name = contact_name.strip()
        update_fields.append("contact_name")
    if email is not None:
        supplier.email = email.strip()
        update_fields.append("email")
    if phone is not None:
        supplier.phone = phone.strip()
        update_fields.append("phone")
    if address_line1 is not None:
        supplier.address_line1 = address_line1.strip()
        update_fields.append("address_line1")
    if address_line2 is not None:
        supplier.address_line2 = address_line2.strip()
        update_fields.append("address_line2")
    if city is not None:
        supplier.city = city.strip()
        update_fields.append("city")
    if postal_code is not None:
        supplier.postal_code = postal_code.strip()
        update_fields.append("postal_code")
    if country is not None:
        supplier.country = country.strip()
        update_fields.append("country")
    if currency is not None and currency.strip():
        supplier.currency = currency.strip().upper()
        update_fields.append("currency")
    if lead_time_days is not None:
        supplier.lead_time_days = lead_time_days
        update_fields.append("lead_time_days")
    if minimum_order_quantity is not None:
        supplier.minimum_order_quantity = max(1, minimum_order_quantity)
        update_fields.append("minimum_order_quantity")
    if is_active is not None:
        supplier.is_active = bool(is_active)
        update_fields.append("is_active")
    if notes is not None:
        supplier.notes = notes.strip()
        update_fields.append("notes")

    supplier.save(update_fields=update_fields)

    audit_service.record(
        actor=actor,
        domain=AuditEvent.Domain.CATALOG,
        action="supplier.update",
        object_type="procurement.supplier",
        object_id=supplier.pk,
        object_repr=str(supplier),
        reason=f"Updated supplier {supplier.code}",
        metadata={"is_active": supplier.is_active},
    )
    return supplier


# =============================================================================
# Purchase Order Lifecycle
# =============================================================================


@transaction.atomic
def create_purchase_order(
    *,
    supplier: Supplier,
    created_by,
    items: list[dict[str, Any]],
    currency: str = "USD",
    expected_delivery_date=None,
    notes: str = "",
    reference: str = "",
) -> PurchaseOrder:
    """Create a new purchase order in DRAFT status with validated line items.

    Args:
        supplier: Supplier instance. Must be active.
        created_by: Staff user creating the order.
        items: List of dicts: ``[{"variant": ProductVariant, "quantity": int, "unit_cost": Decimal | str}, ...]``
        currency: 3-character ISO currency code.
        expected_delivery_date: Date object or None.
        notes: Internal staff notes.
        reference: Optional explicit reference (auto-generated if empty).
    """
    if not supplier.is_active:
        raise ValidationError(_("Cannot create a purchase order for an inactive supplier."))

    if not items:
        raise ValidationError(_("Purchase order must contain at least one line item."))

    ref = reference.strip() or generate_po_reference()

    po = PurchaseOrder.objects.create(
        reference=ref,
        supplier=supplier,
        status=PurchaseOrderStatus.DRAFT,
        currency=currency.strip().upper() or supplier.currency,
        expected_delivery_date=expected_delivery_date,
        notes=notes.strip(),
        created_by=created_by,
    )

    seen_variants = set()
    for item_data in items:
        variant = item_data.get("variant")
        if not variant or not isinstance(variant, ProductVariant):
            raise ValidationError(_("Valid product variant is required for all line items."))
        if variant.pk in seen_variants:
            raise ValidationError(
                _("Variant %(sku)s is duplicated in the order lines.") % {"sku": variant.sku}
            )
        seen_variants.add(variant.pk)

        qty = int(item_data.get("quantity", 0))
        if qty < 1:
            raise ValidationError(
                _("Ordered quantity for %(sku)s must be at least 1.") % {"sku": variant.sku}
            )

        # Cost resolution: explicit cost -> SupplierVariant cost -> variant.cost_price -> 0.00
        cost_raw = item_data.get("unit_cost")
        if cost_raw is not None:
            unit_cost = Decimal(str(cost_raw))
        else:
            sv = SupplierVariant.objects.filter(supplier=supplier, variant=variant).first()
            if sv:
                unit_cost = sv.purchase_cost
            elif getattr(variant, "cost_price", None):
                unit_cost = Decimal(str(variant.cost_price))
            else:
                unit_cost = Decimal("0.00")

        if unit_cost < Decimal("0.00"):
            raise ValidationError(_("Unit cost cannot be negative."))

        PurchaseOrderItem.objects.create(
            purchase_order=po,
            variant=variant,
            ordered_quantity=qty,
            unit_cost=unit_cost,
            notes=str(item_data.get("notes", "")).strip(),
        )

    audit_service.record(
        actor=created_by,
        domain=AuditEvent.Domain.INVENTORY,
        action="purchase_order.create",
        object_type="procurement.purchase_order",
        object_id=po.reference,
        object_repr=str(po),
        reason="Created draft purchase order",
        metadata={
            "supplier_id": supplier.pk,
            "items_count": len(items),
            "total_units": po.total_ordered_units,
            "total_cost": str(po.total_cost),
        },
    )
    return po


@transaction.atomic
def update_purchase_order(
    po: PurchaseOrder,
    *,
    items: list[dict[str, Any]] | None = None,
    expected_delivery_date=None,
    notes: str | None = None,
    user=None,
) -> PurchaseOrder:
    """Update line items, delivery date, or notes on a draft or pending purchase order."""
    if not po.can_edit_items:
        raise OrderImmutableError(
            _("Cannot modify purchase order %(ref)s because it is in %(status)s status.")
            % {"ref": po.reference, "status": po.get_status_display()}
        )

    update_fields = ["updated_at"]

    if expected_delivery_date is not None:
        po.expected_delivery_date = expected_delivery_date
        update_fields.append("expected_delivery_date")

    if notes is not None:
        po.notes = notes.strip()
        update_fields.append("notes")

    if items is not None:
        if not items:
            raise ValidationError(_("Purchase order must contain at least one line item."))

        po.items.all().delete()
        seen_variants = set()
        for item_data in items:
            variant = item_data.get("variant")
            if not variant or not isinstance(variant, ProductVariant):
                raise ValidationError(_("Valid product variant is required for all line items."))
            if variant.pk in seen_variants:
                raise ValidationError(
                    _("Variant %(sku)s is duplicated.") % {"sku": variant.sku}
                )
            seen_variants.add(variant.pk)

            qty = int(item_data.get("quantity", 0))
            if qty < 1:
                raise ValidationError(
                    _("Ordered quantity for %(sku)s must be at least 1.") % {"sku": variant.sku}
                )

            cost_raw = item_data.get("unit_cost")
            if cost_raw is not None:
                unit_cost = Decimal(str(cost_raw))
            else:
                sv = SupplierVariant.objects.filter(supplier=po.supplier, variant=variant).first()
                unit_cost = sv.purchase_cost if sv else Decimal("0.00")

            if unit_cost < Decimal("0.00"):
                raise ValidationError(_("Unit cost cannot be negative."))

            PurchaseOrderItem.objects.create(
                purchase_order=po,
                variant=variant,
                ordered_quantity=qty,
                unit_cost=unit_cost,
                notes=str(item_data.get("notes", "")).strip(),
            )

    po.save(update_fields=update_fields)

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.INVENTORY,
        action="purchase_order.update",
        object_type="procurement.purchase_order",
        object_id=po.reference,
        object_repr=str(po),
        reason="Updated purchase order details",
        metadata={
            "items_count": po.items.count(),
            "total_units": po.total_ordered_units,
            "total_cost": str(po.total_cost),
        },
    )
    return po


@transaction.atomic
def submit_for_approval(po: PurchaseOrder, *, user) -> PurchaseOrder:
    """Submit a draft purchase order for managerial / finance approval."""
    if po.status != PurchaseOrderStatus.DRAFT:
        raise InvalidStatusTransition(
            _("Only draft purchase orders can be submitted for approval (current: %(st)s).")
            % {"st": po.get_status_display()}
        )

    if not po.items.exists():
        raise ValidationError(_("Cannot submit an empty purchase order for approval."))

    total_units = po.total_ordered_units
    if total_units < po.supplier.minimum_order_quantity:
        raise ValidationError(
            _(
                "Purchase order total units (%(units)d) does not meet supplier minimum order quantity (%(moq)d)."
            )
            % {"units": total_units, "moq": po.supplier.minimum_order_quantity}
        )

    po.status = PurchaseOrderStatus.PENDING_APPROVAL
    po.save(update_fields=["status", "updated_at"])

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.INVENTORY,
        action="purchase_order.submit",
        object_type="procurement.purchase_order",
        object_id=po.reference,
        object_repr=str(po),
        reason="Submitted purchase order for approval",
        metadata={"total_cost": str(po.total_cost), "total_units": total_units},
    )
    return po


@transaction.atomic
def approve_purchase_order(po: PurchaseOrder, *, user) -> PurchaseOrder:
    """Approve a purchase order ready for transmission to the supplier."""
    if po.status not in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.PENDING_APPROVAL):
        raise InvalidStatusTransition(
            _("Cannot approve purchase order in %(st)s status.") % {"st": po.get_status_display()}
        )

    if not po.items.exists():
        raise ValidationError(_("Cannot approve an empty purchase order."))

    po.status = PurchaseOrderStatus.APPROVED
    po.approved_by = user
    po.approved_at = timezone.now()
    po.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.INVENTORY,
        action="purchase_order.approve",
        object_type="procurement.purchase_order",
        object_id=po.reference,
        object_repr=str(po),
        reason="Approved purchase order",
        metadata={"approved_by": getattr(user, "email", str(user)), "total_cost": str(po.total_cost)},
    )
    return po


@transaction.atomic
def send_purchase_order(po: PurchaseOrder, *, user) -> PurchaseOrder:
    """Record that the purchase order was dispatched/sent to the supplier."""
    if po.status != PurchaseOrderStatus.APPROVED:
        raise InvalidStatusTransition(
            _("Only approved purchase orders can be marked sent (current: %(st)s).")
            % {"st": po.get_status_display()}
        )

    po.status = PurchaseOrderStatus.ORDERED
    po.sent_at = timezone.now()
    po.save(update_fields=["status", "sent_at", "updated_at"])

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.INVENTORY,
        action="purchase_order.send",
        object_type="procurement.purchase_order",
        object_id=po.reference,
        object_repr=str(po),
        reason="Dispatched purchase order to supplier",
        metadata={"supplier": po.supplier.name, "sent_at": po.sent_at.isoformat()},
    )
    return po


@transaction.atomic
def cancel_purchase_order(po: PurchaseOrder, *, user, reason: str = "") -> PurchaseOrder:
    """Cancel an active purchase order."""
    if po.status in (PurchaseOrderStatus.FULLY_RECEIVED, PurchaseOrderStatus.CANCELLED):
        raise InvalidStatusTransition(
            _("Cannot cancel a %(st)s purchase order.") % {"st": po.get_status_display()}
        )

    po.status = PurchaseOrderStatus.CANCELLED
    po.cancelled_by = user
    po.cancelled_at = timezone.now()
    po.cancellation_reason = reason.strip()
    po.save(
        update_fields=[
            "status",
            "cancelled_by",
            "cancelled_at",
            "cancellation_reason",
            "updated_at",
        ]
    )

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.INVENTORY,
        action="purchase_order.cancel",
        object_type="procurement.purchase_order",
        object_id=po.reference,
        object_repr=str(po),
        reason=reason or "Cancelled purchase order",
        metadata={"cancellation_reason": reason},
    )
    return po


# =============================================================================
# Goods Receiving & Inventory Integration
# =============================================================================


@transaction.atomic
def receive_purchase_order_delivery(
    po: PurchaseOrder,
    *,
    delivery_reference: str,
    items: list[dict[str, Any]] | None = None,
    receipt_items_data: list[dict[str, Any]] | None = None,
    user,
    notes: str = "",
    idempotency_key: str = "",
) -> PurchaseOrderReceipt:
    """Receive a physical shipment against an active purchase order."""
    items = items if items is not None else (receipt_items_data or [])
    delivery_ref = delivery_reference.strip()
    if not delivery_ref:
        raise ValidationError(_("Delivery reference / tracking number is required."))

    # Idempotency check: duplicate delivery reference for this PO
    if PurchaseOrderReceipt.objects.filter(
        purchase_order=po, delivery_reference=delivery_ref
    ).exists():
        raise DuplicateReceiptError(
            _("Delivery reference %(ref)s has already been received for purchase order %(po)s.")
            % {"ref": delivery_ref, "po": po.reference}
        )

    if idempotency_key and PurchaseOrderReceipt.objects.filter(idempotency_key=idempotency_key).exists():
        return PurchaseOrderReceipt.objects.get(idempotency_key=idempotency_key)

    # 1. Lock PO and verify status
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if not po.can_receive:
        raise InvalidStatusTransition(
            _("Purchase order %(po)s cannot receive goods in %(status)s status.")
            % {"po": po.reference, "status": po.get_status_display()}
        )

    if not items:
        raise ValidationError(_("At least one line item must be received."))

    # 2. Lock and map line items
    line_items_map = {item.pk: item for item in po.items.select_for_update().all()}

    validated_receipt_items: list[dict[str, Any]] = []
    total_accepted_units = 0
    total_damaged_units = 0

    for entry in items:
        line_item_id = entry.get("line_item_id") or getattr(entry.get("line_item"), "pk", None)
        line_item = line_items_map.get(line_item_id)
        if not line_item:
            raise ValidationError(
                _("Line item %(id)s does not belong to purchase order %(po)s.")
                % {"id": line_item_id, "po": po.reference}
            )

        qty_received = max(0, int(entry.get("quantity_received", 0)))
        qty_damaged = max(0, int(entry.get("quantity_damaged", 0)))
        qty_total = qty_received + qty_damaged

        if qty_total == 0:
            continue  # No units received for this line in this delivery

        outstanding = line_item.outstanding_quantity
        if qty_total > outstanding:
            raise OverReceiptError(
                _(
                    "Cannot receive %(total)d units (%(acc)d accepted, %(dmg)d damaged) "
                    "for %(sku)s. Only %(out)d units remain outstanding."
                )
                % {
                    "total": qty_total,
                    "acc": qty_received,
                    "dmg": qty_damaged,
                    "sku": line_item.variant.sku,
                    "out": outstanding,
                }
            )

        validated_receipt_items.append(
            {
                "line_item": line_item,
                "quantity_received": qty_received,
                "quantity_damaged": qty_damaged,
                "notes": str(entry.get("notes", "")).strip(),
            }
        )
        total_accepted_units += qty_received
        total_damaged_units += qty_damaged

    if not validated_receipt_items:
        raise ValidationError(_("No quantities specified for delivery receipt."))

    # 3. Create the goods receipt header
    receipt = PurchaseOrderReceipt.objects.create(
        purchase_order=po,
        delivery_reference=delivery_ref,
        received_by=user,
        received_at=timezone.now(),
        notes=notes.strip(),
        idempotency_key=idempotency_key.strip() if idempotency_key else "",
    )

    # 4. Process each item: record movement & update line item
    for entry in validated_receipt_items:
        line_item = entry["line_item"]
        qty_acc = entry["quantity_received"]
        qty_dmg = entry["quantity_damaged"]
        item_notes = entry["notes"]

        # Only accepted (sellable) goods are credited into Stock.on_hand
        if qty_acc > 0:
            adjust_stock(
                variant=line_item.variant,
                delta=qty_acc,
                kind=InventoryMovement.Kind.RECEIVED,
                user=user,
                reference=f"po:{po.reference}:{delivery_ref}",
                note=f"PO {po.reference} delivery receipt ({delivery_ref})",
            )
            line_item.received_quantity += qty_acc
            line_item.save(update_fields=["received_quantity", "updated_at"])

        PurchaseOrderReceiptItem.objects.create(
            receipt=receipt,
            line_item=line_item,
            quantity_received=qty_acc,
            quantity_damaged=qty_dmg,
            notes=item_notes,
        )

    # 5. Evaluate and update purchase order status
    po_items = list(po.items.all())
    all_fulfilled = all(item.received_quantity >= item.ordered_quantity for item in po_items)
    any_received = any(item.received_quantity > 0 for item in po_items)

    new_status = po.status
    if all_fulfilled:
        new_status = PurchaseOrderStatus.FULLY_RECEIVED
    elif any_received or po.status == PurchaseOrderStatus.APPROVED:
        new_status = PurchaseOrderStatus.PARTIALLY_RECEIVED

    if new_status != po.status:
        po.status = new_status
        po.save(update_fields=["status", "updated_at"])

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.INVENTORY,
        action="purchase_order.receive",
        object_type="procurement.purchase_order",
        object_id=po.reference,
        object_repr=str(po),
        reason=f"Received delivery {delivery_ref}",
        metadata={
            "delivery_reference": delivery_ref,
            "receipt_id": receipt.pk,
            "units_accepted": total_accepted_units,
            "units_damaged": total_damaged_units,
            "new_status": po.status,
        },
    )
    return receipt


# =============================================================================
# Phase 46: Supplier Invoices & Accounts Payable Intelligence Services
# =============================================================================


def normalize_invoice_number(value: str) -> str:
    """Normalize supplier invoice identifier to prevent duplicate submissions.

    Removes leading/trailing whitespace, converts to uppercase, and strips non-alphanumeric
    punctuation while preserving alphanumeric characters (e.g. ' Inv - 2026/01 ' -> 'INV202601').
    """
    if not value:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(value).strip()).upper()
    return cleaned or str(value).strip().upper()


def generate_payment_reference() -> str:
    """Generate a unique sequential AP payment disbursement reference.

    Format: PAY-AP-YYYYMMDD-XXXX (e.g. PAY-AP-20261013-0001).
    """
    date_str = timezone.now().strftime("%Y%m%d")
    prefix = f"PAY-AP-{date_str}-"
    latest = (
        SupplierPayment.objects.filter(payment_reference__startswith=prefix)
        .order_by("-payment_reference")
        .values_list("payment_reference", flat=True)
        .first()
    )
    if latest:
        try:
            seq = int(latest.split("-")[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"


def create_supplier_invoice(
    supplier: Supplier,
    invoice_number: str,
    invoice_date: Any,
    due_date: Any = None,
    currency: str = "USD",
    purchase_order: PurchaseOrder | None = None,
    supplier_reference: str = "",
    tax_amount: Decimal = Decimal("0.00"),
    shipping_amount: Decimal = Decimal("0.00"),
    discount_amount: Decimal = Decimal("0.00"),
    items_data: list[dict[str, Any]] | None = None,
    lines_data: list[dict[str, Any]] | None = None,
    attachment: Any = None,
    internal_notes: str = "",
    notes: str = "",
    is_duplicate_override: bool = False,
    duplicate_override_reason: str = "",
    user: Any = None,
) -> SupplierInvoice:
    """Create a new supplier invoice in Draft status with line items and duplicate guards."""
    if not supplier.is_active:
        raise ValidationError(_("Cannot record an invoice for an inactive supplier."))

    items_data = items_data if items_data is not None else (lines_data or [])
    internal_notes = internal_notes or notes
    if due_date is None and invoice_date:
        due_date = invoice_date + timezone.timedelta(days=30)

    invoice_number_str = str(invoice_number).strip()
    if not invoice_number_str:
        raise ValidationError({"invoice_number": _("Invoice number is required.")})

    normalized_num = normalize_invoice_number(invoice_number_str)

    # Duplicate check:
    duplicate_qs = SupplierInvoice.objects.filter(
        supplier=supplier,
        normalized_invoice_number=normalized_num,
    ).exclude(status=SupplierInvoiceStatus.VOIDED)

    if duplicate_qs.exists() and not is_duplicate_override:
        existing = duplicate_qs.first()
        raise DuplicateInvoiceError(
            f"An active invoice with number '{existing.invoice_number}' already exists for supplier {supplier.name}."
        )

    if is_duplicate_override and not duplicate_override_reason.strip():
        raise ValidationError(_("A valid reason is required when overriding duplicate invoice detection."))

    if due_date and invoice_date and due_date < invoice_date:
        raise ValidationError({"due_date": _("Due date cannot precede the invoice date.")})

    tax_amount = Decimal(str(tax_amount or "0.00"))
    shipping_amount = Decimal(str(shipping_amount or "0.00"))
    discount_amount = Decimal(str(discount_amount or "0.00"))

    if tax_amount < Decimal("0.00"):
        raise ValidationError({"tax_amount": _("Tax amount cannot be negative.")})
    if shipping_amount < Decimal("0.00"):
        raise ValidationError({"shipping_amount": _("Shipping amount cannot be negative.")})
    if discount_amount < Decimal("0.00"):
        raise ValidationError({"discount_amount": _("Discount amount cannot be negative.")})

    # Validate purchase order supplier alignment
    if purchase_order and purchase_order.supplier_id != supplier.pk:
        raise ValidationError({"purchase_order": _("Linked purchase order does not belong to this supplier.")})

    # Validate items
    subtotal = Decimal("0.00")
    validated_lines: list[dict[str, Any]] = []

    if items_data:
        for idx, item in enumerate(items_data):
            description = str(item.get("description", "")).strip()
            qty = int(item.get("quantity", 0))
            unit_cost = Decimal(str(item.get("unit_cost", "0.00")))
            po_item = item.get("po_item")
            variant = item.get("variant")

            if not description and po_item:
                description = f"{po_item.variant.product.name} ({po_item.variant.sku})"
            elif not description:
                description = f"Line Item {idx + 1}"

            if qty <= 0:
                raise ValidationError({f"items_{idx}_quantity": _("Quantity must be at least 1.")})
            if unit_cost < Decimal("0.00"):
                raise ValidationError({f"items_{idx}_unit_cost": _("Unit cost cannot be negative.")})

            line_tot = unit_cost * Decimal(qty)
            subtotal += line_tot

            validated_lines.append({
                "description": description,
                "quantity": qty,
                "unit_cost": unit_cost,
                "line_total": line_tot,
                "po_item": po_item,
                "variant": variant or (po_item.variant if po_item else None),
                "notes": str(item.get("notes", "")).strip(),
            })

    total_amount = max(Decimal("0.00"), subtotal + tax_amount + shipping_amount - discount_amount)

    with transaction.atomic():
        invoice = SupplierInvoice.objects.create(
            supplier=supplier,
            purchase_order=purchase_order,
            invoice_number=invoice_number_str,
            normalized_invoice_number=normalized_num,
            supplier_reference=supplier_reference.strip(),
            status=SupplierInvoiceStatus.DRAFT,
            invoice_date=invoice_date,
            received_date=timezone.localdate(),
            due_date=due_date,
            currency=currency.upper(),
            subtotal=subtotal,
            tax_amount=tax_amount,
            shipping_amount=shipping_amount,
            discount_amount=discount_amount,
            total_amount=total_amount,
            amount_paid=Decimal("0.00"),
            amount_credited=Decimal("0.00"),
            is_duplicate_override=is_duplicate_override,
            duplicate_override_reason=duplicate_override_reason.strip(),
            attachment=attachment,
            internal_notes=internal_notes.strip(),
            created_by=user,
        )

        for line_data in validated_lines:
            SupplierInvoiceLine.objects.create(
                invoice=invoice,
                description=line_data["description"],
                quantity=line_data["quantity"],
                unit_cost=line_data["unit_cost"],
                line_total=line_data["line_total"],
                po_item=line_data["po_item"],
                variant=line_data["variant"],
                notes=line_data["notes"],
            )

        if purchase_order:
            perform_three_way_match(invoice)

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.PAYMENTS,
        action="invoice.create",
        object_type="procurement.supplier_invoice",
        object_id=str(invoice.pk),
        object_repr=str(invoice),
        reason=f"Created draft supplier bill {invoice.invoice_number}",
        metadata={
            "supplier_id": supplier.pk,
            "invoice_number": invoice.invoice_number,
            "total_amount": str(invoice.total_amount),
            "purchase_order_id": purchase_order.pk if purchase_order else None,
        },
    )
    return invoice


def update_supplier_invoice(
    invoice: SupplierInvoice,
    invoice_number: str | None = None,
    invoice_date: Any = None,
    due_date: Any = None,
    currency: str | None = None,
    purchase_order: PurchaseOrder | None = None,
    supplier_reference: str | None = None,
    tax_amount: Decimal | None = None,
    shipping_amount: Decimal | None = None,
    discount_amount: Decimal | None = None,
    items_data: list[dict[str, Any]] | None = None,
    lines_data: list[dict[str, Any]] | None = None,
    attachment: Any = None,
    internal_notes: str | None = None,
    notes: str | None = None,
    user: Any = None,
) -> SupplierInvoice:
    """Update a draft supplier invoice and its line items."""
    if invoice.status != SupplierInvoiceStatus.DRAFT:
        raise OrderImmutableError(f"Cannot edit invoice in '{invoice.status}' status. Only draft invoices may be edited.")

    if items_data is None and lines_data is not None:
        items_data = lines_data
    if internal_notes is None and notes is not None:
        internal_notes = notes

    if invoice_number is not None:
        inv_str = invoice_number.strip()
        if not inv_str:
            raise ValidationError({"invoice_number": _("Invoice number is required.")})
        norm = normalize_invoice_number(inv_str)
        dup_qs = SupplierInvoice.objects.filter(
            supplier=invoice.supplier,
            normalized_invoice_number=norm,
        ).exclude(pk=invoice.pk).exclude(status=SupplierInvoiceStatus.VOIDED)
        if dup_qs.exists() and not invoice.is_duplicate_override:
            raise DuplicateInvoiceError(f"An invoice with number '{inv_str}' already exists for this supplier.")
        invoice.invoice_number = inv_str
        invoice.normalized_invoice_number = norm

    if invoice_date is not None:
        invoice.invoice_date = invoice_date
    if due_date is not None:
        invoice.due_date = due_date
    if invoice.due_date < invoice.invoice_date:
        raise ValidationError({"due_date": _("Due date cannot precede the invoice date.")})

    if currency is not None:
        invoice.currency = currency.upper()
    if purchase_order is not None:
        if purchase_order.supplier_id != invoice.supplier_id:
            raise ValidationError({"purchase_order": _("Linked purchase order does not belong to this supplier.")})
        invoice.purchase_order = purchase_order
    if supplier_reference is not None:
        invoice.supplier_reference = supplier_reference.strip()
    if tax_amount is not None:
        invoice.tax_amount = Decimal(str(tax_amount))
    if shipping_amount is not None:
        invoice.shipping_amount = Decimal(str(shipping_amount))
    if discount_amount is not None:
        invoice.discount_amount = Decimal(str(discount_amount))
    if internal_notes is not None:
        invoice.internal_notes = internal_notes.strip()
    if attachment:
        invoice.attachment = attachment

    with transaction.atomic():
        if items_data is not None:
            invoice.lines.all().delete()
            subtotal = Decimal("0.00")
            for idx, item in enumerate(items_data):
                description = str(item.get("description", "")).strip()
                qty = int(item.get("quantity", 0))
                unit_cost = Decimal(str(item.get("unit_cost", "0.00")))
                po_item = item.get("po_item")
                variant = item.get("variant")

                if not description and po_item:
                    description = f"{po_item.variant.product.name} ({po_item.variant.sku})"
                elif not description:
                    description = f"Line Item {idx + 1}"

                if qty <= 0:
                    raise ValidationError({f"items_{idx}_quantity": _("Quantity must be at least 1.")})
                if unit_cost < Decimal("0.00"):
                    raise ValidationError({f"items_{idx}_unit_cost": _("Unit cost cannot be negative.")})

                line_tot = unit_cost * Decimal(qty)
                subtotal += line_tot
                SupplierInvoiceLine.objects.create(
                    invoice=invoice,
                    description=description,
                    quantity=qty,
                    unit_cost=unit_cost,
                    line_total=line_tot,
                    po_item=po_item,
                    variant=variant or (po_item.variant if po_item else None),
                    notes=str(item.get("notes", "")).strip(),
                )
            invoice.subtotal = subtotal

        invoice.total_amount = max(
            Decimal("0.00"),
            invoice.subtotal + invoice.tax_amount + invoice.shipping_amount - invoice.discount_amount,
        )
        invoice.save()

        if invoice.purchase_order:
            perform_three_way_match(invoice)

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.PAYMENTS,
        action="invoice.update",
        object_type="procurement.supplier_invoice",
        object_id=str(invoice.pk),
        object_repr=str(invoice),
        reason=f"Updated draft supplier bill {invoice.invoice_number}",
        metadata={"total_amount": str(invoice.total_amount)},
    )
    return invoice


def submit_supplier_invoice(invoice: SupplierInvoice, user: Any = None) -> SupplierInvoice:
    """Submit a draft supplier invoice for approval and initiate 3-way matching."""
    if invoice.status != SupplierInvoiceStatus.DRAFT:
        raise InvalidStatusTransition(f"Cannot submit invoice in '{invoice.status}' status. Only draft invoices can be submitted.")

    if not invoice.lines.exists() and invoice.total_amount == Decimal("0.00"):
        raise ValidationError(_("Cannot submit an empty invoice with zero total."))

    with transaction.atomic():
        invoice.status = SupplierInvoiceStatus.SUBMITTED
        invoice.save(update_fields=["status", "updated_at"])

        if invoice.purchase_order:
            perform_three_way_match(invoice)

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.PAYMENTS,
        action="invoice.submit",
        object_type="procurement.supplier_invoice",
        object_id=str(invoice.pk),
        object_repr=str(invoice),
        reason=f"Submitted supplier invoice {invoice.invoice_number} for approval",
        metadata={"total_amount": str(invoice.total_amount)},
    )
    return invoice


def perform_three_way_match(
    invoice: SupplierInvoice,
    tolerance_price: Decimal = Decimal("0.00"),
    tolerance_qty: int = 0,
) -> InvoiceMatchRecord:
    """Execute three-way matching between purchase order, goods receipts, and invoice."""
    po = invoice.purchase_order
    if not po:
        record, _ = InvoiceMatchRecord.objects.update_or_create(
            invoice=invoice,
            defaults={
                "purchase_order": None,
                "status": InvoiceMatchingStatus.UNMATCHED,
                "price_variance_total": Decimal("0.00"),
                "quantity_variance_total": 0,
                "is_within_tolerance": False,
                "summary": "No purchase order linked to this invoice. Manual accounts payable review required.",
                "discrepancy_details": [],
                "performed_at": timezone.now(),
            },
        )
        return record

    discrepancies: list[dict[str, Any]] = []
    total_price_variance = Decimal("0.00")
    total_qty_variance = 0
    has_price_variance = False
    has_missing_receipt = False
    has_overbill = False
    has_quantity_variance = False

    lines = list(invoice.lines.all())
    for line in lines:
        if line.po_item:
            po_item = line.po_item
            agreed_cost = po_item.unit_cost
            invoiced_cost = line.unit_cost
            cost_diff = invoiced_cost - agreed_cost
            line_price_var = cost_diff * Decimal(line.quantity)
            total_price_variance += line_price_var

            if abs(cost_diff) > tolerance_price:
                has_price_variance = True
                discrepancies.append({
                    "kind": "price_variance",
                    "type": "price_variance",
                    "line_id": line.pk,
                    "description": line.description,
                    "invoiced_unit_cost": str(invoiced_cost),
                    "agreed_po_unit_cost": str(agreed_cost),
                    "variance_per_unit": str(cost_diff),
                    "total_line_variance": str(line_price_var),
                })

            # Calculate cumulative billed across all non-voided invoices for this PO item
            prior_billed = (
                SupplierInvoiceLine.objects.filter(
                    po_item=po_item,
                    invoice__supplier=invoice.supplier,
                )
                .exclude(invoice=invoice)
                .exclude(invoice__status=SupplierInvoiceStatus.VOIDED)
                .aggregate(Sum("quantity"))["quantity__sum"]
                or 0
            )
            cumulative_billed = prior_billed + line.quantity
            ordered_qty = po_item.ordered_quantity
            received_qty = po_item.received_quantity

            if cumulative_billed > ordered_qty + tolerance_qty:
                has_overbill = True
                qty_over = cumulative_billed - ordered_qty
                total_qty_variance += qty_over
                discrepancies.append({
                    "kind": "quantity_overbill",
                    "type": "overbilled_ordered",
                    "line_id": line.pk,
                    "description": line.description,
                    "ordered_qty": ordered_qty,
                    "prior_billed_qty": prior_billed,
                    "invoiced_qty": line.quantity,
                    "cumulative_billed_qty": cumulative_billed,
                    "over_quantity": qty_over,
                })

            if cumulative_billed > received_qty + tolerance_qty:
                has_missing_receipt = True
                qty_unreceived = cumulative_billed - received_qty
                total_qty_variance += qty_unreceived
                discrepancies.append({
                    "kind": "missing_receipt",
                    "type": "unreceived_goods",
                    "line_id": line.pk,
                    "description": line.description,
                    "received_qty": received_qty,
                    "prior_billed_qty": prior_billed,
                    "invoiced_qty": line.quantity,
                    "cumulative_billed_qty": cumulative_billed,
                    "unreceived_quantity": qty_unreceived,
                })
        else:
            # Line not mapped to PO line item
            discrepancies.append({
                "kind": "unlinked_line",
                "type": "unlinked_line",
                "line_id": line.pk,
                "description": line.description,
                "quantity": line.quantity,
                "unit_cost": str(line.unit_cost),
                "notes": "Line item does not correspond to any purchase order line item.",
            })
            has_price_variance = True

    # Determine overall matching status
    if has_overbill:
        match_status = InvoiceMatchingStatus.OVERBILLED
        summary = f"Overbilled: Cumulative billed quantities exceed authorized purchase order ordered quantities."
    elif has_missing_receipt:
        match_status = InvoiceMatchingStatus.MISSING_RECEIPT
        summary = f"Missing goods receipts: Invoiced quantities exceed accepted warehouse deliveries by {total_qty_variance} units."
    elif has_price_variance:
        match_status = InvoiceMatchingStatus.VARIANCE_PRICE
        summary = f"Price variance detected: Net price variance of {invoice.currency} {total_price_variance} against agreed PO pricing."
    elif has_quantity_variance:
        match_status = InvoiceMatchingStatus.VARIANCE_QUANTITY
        summary = "Quantity variance detected between invoiced lines and purchase order."
    else:
        match_status = InvoiceMatchingStatus.MATCHED
        summary = "Three-way match successful. Invoiced quantities match accepted goods receipts and agreed purchase order pricing."

    is_within_tol = len(discrepancies) == 0

    record, _ = InvoiceMatchRecord.objects.update_or_create(
        invoice=invoice,
        defaults={
            "purchase_order": po,
            "status": match_status,
            "price_variance_total": total_price_variance,
            "quantity_variance_total": total_qty_variance,
            "is_within_tolerance": is_within_tol,
            "summary": summary,
            "discrepancy_details": discrepancies,
            "performed_at": timezone.now(),
        },
    )
    invoice.match_record = record
    return record


def approve_supplier_invoice(
    invoice: SupplierInvoice,
    user: Any,
    override_discrepancies: bool = False,
    override_reason: str = "",
    discrepancy_override_reason: str = "",
) -> SupplierInvoice:
    """Approve a supplier invoice, recognizing accounts payable liability."""
    override_reason = override_reason or discrepancy_override_reason
    if invoice.status not in (SupplierInvoiceStatus.SUBMITTED, SupplierInvoiceStatus.DISPUTED):
        raise InvalidStatusTransition(
            f"Cannot approve invoice in '{invoice.status}' status. Only submitted or disputed invoices can be approved."
        )

    # Enforce separation of duties: preparer cannot approve their own invoice
    user_pk = getattr(user, "pk", None)
    is_super = getattr(user, "is_superuser", False)
    if invoice.created_by_id and invoice.created_by_id == user_pk and not is_super:
        raise SeparationOfDutiesError(
            _("Separation of duties: you cannot approve your own supplier invoice.")
        )

    with transaction.atomic():
        # Evaluate 3-way matching
        if invoice.purchase_order:
            match_record = perform_three_way_match(invoice)
            if match_record.status != InvoiceMatchingStatus.MATCHED and not match_record.is_within_tolerance:
                if not override_discrepancies:
                    raise DiscrepancyApprovalRequired(
                        f"Cannot approve invoice: 3-way match failed with '{match_record.get_status_display()}'. {match_record.summary}"
                    )
                if not override_reason.strip():
                    raise ValidationError(_("A documented reason is required to override matching discrepancies."))
                match_record.override_approved = True
                match_record.override_reason = override_reason.strip()
                match_record.reviewed_by = user
                match_record.reviewed_at = timezone.now()
                match_record.save(update_fields=["override_approved", "override_reason", "reviewed_by", "reviewed_at"])
                invoice.match_record = match_record

        invoice.status = SupplierInvoiceStatus.APPROVED
        invoice.approved_by = user
        invoice.approved_at = timezone.now()
        invoice.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.PAYMENTS,
        action="invoice.approve",
        object_type="procurement.supplier_invoice",
        object_id=str(invoice.pk),
        object_repr=str(invoice),
        reason=f"Approved supplier invoice {invoice.invoice_number} (liability recognized: {invoice.currency} {invoice.total_amount})",
        metadata={
            "total_amount": str(invoice.total_amount),
            "override_discrepancies": override_discrepancies,
        },
    )
    return invoice


def dispute_supplier_invoice(invoice: SupplierInvoice, user: Any, reason: str) -> SupplierInvoice:
    """Mark a supplier invoice as disputed due to price/quantity or supplier issues."""
    if invoice.status not in (SupplierInvoiceStatus.SUBMITTED, SupplierInvoiceStatus.APPROVED):
        raise InvalidStatusTransition(f"Cannot dispute invoice in '{invoice.status}' status.")

    reason_str = str(reason).strip()
    if not reason_str:
        raise ValidationError(_("A dispute reason is required."))

    invoice.status = SupplierInvoiceStatus.DISPUTED
    invoice.dispute_reason = reason_str
    invoice.save(update_fields=["status", "dispute_reason", "updated_at"])

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.PAYMENTS,
        action="invoice.dispute",
        object_type="procurement.supplier_invoice",
        object_id=str(invoice.pk),
        object_repr=str(invoice),
        reason=f"Disputed supplier invoice {invoice.invoice_number}: {reason_str}",
        metadata={"dispute_reason": reason_str},
    )
    return invoice


def void_supplier_invoice(invoice: SupplierInvoice, user: Any, reason: str) -> SupplierInvoice:
    """Void a supplier invoice with full audit trail, reversing any unapproved liability."""
    if not invoice.can_void:
        raise InvalidInvoiceOperationError(
            f"Cannot void invoice {invoice.invoice_number}. Invoices with applied payments or credits cannot be voided directly."
        )

    reason_str = str(reason).strip()
    if not reason_str:
        raise ValidationError(_("A void reason is required."))

    invoice.status = SupplierInvoiceStatus.VOIDED
    invoice.voided_by = user
    invoice.voided_at = timezone.now()
    invoice.void_reason = reason_str
    invoice.save(update_fields=["status", "voided_by", "voided_at", "void_reason", "updated_at"])

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.PAYMENTS,
        action="invoice.void",
        object_type="procurement.supplier_invoice",
        object_id=str(invoice.pk),
        object_repr=str(invoice),
        reason=f"Voided supplier invoice {invoice.invoice_number}: {reason_str}",
        metadata={"void_reason": reason_str},
    )
    return invoice


def record_supplier_payment(
    supplier: Supplier,
    amount: Decimal,
    payment_method: str = PaymentMethod.BANK_TRANSFER,
    payment_date: Any = None,
    currency: str | None = None,
    bank_transaction_reference: str = "",
    allocations_data: list[dict[str, Any]] | None = None,
    notes: str = "",
    user: Any = None,
) -> SupplierPayment:
    """Record internal disbursement settling one or more approved supplier invoices."""
    amount_dec = Decimal(str(amount))
    if amount_dec <= Decimal("0.00"):
        raise ValidationError({"amount": _("Payment amount must be greater than zero.")})

    payment_date = payment_date or timezone.localdate()
    ref = generate_payment_reference()
    payment_currency = (currency or supplier.currency).upper()

    with transaction.atomic():
        payment = SupplierPayment.objects.create(
            supplier=supplier,
            payment_reference=ref,
            bank_transaction_reference=bank_transaction_reference.strip(),
            payment_method=payment_method,
            payment_date=payment_date,
            amount=amount_dec,
            currency=payment_currency,
            status=SupplierPaymentStatus.COMPLETED,
            recorded_by=user,
            notes=notes.strip(),
        )

        total_allocated = Decimal("0.00")
        if allocations_data:
            for item in allocations_data:
                inv_id = item.get("invoice_id") or getattr(item.get("invoice"), "pk", None)
                alloc_amt = Decimal(str(item.get("amount", "0.00")))
                if alloc_amt <= Decimal("0.00"):
                    continue

                invoice = SupplierInvoice.objects.select_for_update().get(pk=inv_id)
                if invoice.supplier_id != supplier.pk:
                    raise ValidationError(_("Cannot allocate payment to an invoice belonging to a different supplier."))

                if invoice.status not in (SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID):
                    raise InvalidInvoiceOperationError(
                        f"Cannot allocate payment to invoice {invoice.invoice_number} in '{invoice.status}' status. Only approved invoices may be settled."
                    )

                if alloc_amt > invoice.outstanding_balance:
                    raise InsufficientInvoiceBalanceError(
                        f"Allocated amount {alloc_amt} exceeds outstanding balance {invoice.outstanding_balance} on invoice {invoice.invoice_number}."
                    )

                SupplierPaymentAllocation.objects.create(
                    payment=payment,
                    invoice=invoice,
                    amount=alloc_amt,
                )
                total_allocated += alloc_amt

                invoice.amount_paid += alloc_amt
                if invoice.outstanding_balance == Decimal("0.00"):
                    invoice.status = SupplierInvoiceStatus.PAID
                else:
                    invoice.status = SupplierInvoiceStatus.PARTIALLY_PAID
                invoice.save(update_fields=["amount_paid", "status", "updated_at"])

            if total_allocated > amount_dec:
                raise ValidationError(_("Total allocated amounts cannot exceed the payment amount."))

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.PAYMENTS,
        action="supplier_payment.record",
        object_type="procurement.supplier_payment",
        object_id=payment.payment_reference,
        object_repr=str(payment),
        reason=f"Recorded AP disbursement {payment.payment_reference} ({payment.currency} {payment.amount})",
        metadata={
            "supplier_id": supplier.pk,
            "amount": str(payment.amount),
            "allocated_amount": str(total_allocated),
            "payment_method": payment.payment_method,
        },
    )
    return payment


def reverse_supplier_payment(payment: SupplierPayment, user: Any, reason: str) -> SupplierPayment:
    """Reverse a completed supplier payment, restoring invoice liabilities without destroying history."""
    if payment.status != SupplierPaymentStatus.COMPLETED:
        raise PaymentReversalError(f"Cannot reverse payment in '{payment.status}' status. Only completed payments can be reversed.")

    reason_str = str(reason).strip()
    if not reason_str:
        raise ValidationError(_("A reversal reason is required."))

    with transaction.atomic():
        allocations = list(payment.allocations.select_related("invoice").all())
        for alloc in allocations:
            invoice = SupplierInvoice.objects.select_for_update().get(pk=alloc.invoice_id)
            invoice.amount_paid = max(Decimal("0.00"), invoice.amount_paid - alloc.amount)
            if invoice.outstanding_balance == Decimal("0.00"):
                invoice.status = SupplierInvoiceStatus.PAID
            elif invoice.amount_paid > Decimal("0.00") or invoice.amount_credited > Decimal("0.00"):
                invoice.status = SupplierInvoiceStatus.PARTIALLY_PAID
            else:
                invoice.status = SupplierInvoiceStatus.APPROVED
            invoice.save(update_fields=["amount_paid", "status", "updated_at"])

        payment.status = SupplierPaymentStatus.REVERSED
        payment.reversed_by = user
        payment.reversed_at = timezone.now()
        payment.reversal_reason = reason_str
        payment.save(update_fields=["status", "reversed_by", "reversed_at", "reversal_reason", "updated_at"])

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.PAYMENTS,
        action="supplier_payment.reverse",
        object_type="procurement.supplier_payment",
        object_id=payment.payment_reference,
        object_repr=str(payment),
        reason=f"Reversed AP payment {payment.payment_reference}: {reason_str}",
        metadata={"reversal_reason": reason_str},
    )
    return payment


def create_supplier_credit_note(
    supplier: Supplier,
    credit_note_number: str,
    amount: Decimal,
    reason: str,
    invoice: SupplierInvoice | None = None,
    credit_date: Any = None,
    currency: str = "USD",
    notes: str = "",
    user: Any = None,
) -> SupplierCreditNote:
    """Record an approved credit note from a supplier."""
    amount_dec = Decimal(str(amount))
    if amount_dec <= Decimal("0.00"):
        raise ValidationError({"amount": _("Credit amount must be greater than zero.")})

    cn_str = str(credit_note_number).strip()
    if not cn_str:
        raise ValidationError({"credit_note_number": _("Credit note number is required.")})

    reason_str = str(reason).strip()
    if not reason_str:
        raise ValidationError({"reason": _("A credit note reason is required.")})

    if invoice and invoice.supplier_id != supplier.pk:
        raise ValidationError({"invoice": _("Linked invoice does not belong to this supplier.")})

    credit_date = credit_date or timezone.localdate()

    credit_note = SupplierCreditNote.objects.create(
        supplier=supplier,
        credit_note_number=cn_str,
        invoice=invoice,
        credit_date=credit_date,
        amount=amount_dec,
        amount_applied=Decimal("0.00"),
        currency=currency.upper(),
        status=CreditNoteStatus.APPROVED,
        reason=reason_str,
        notes=notes.strip(),
        created_by=user,
    )

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.PAYMENTS,
        action="credit_note.create",
        object_type="procurement.supplier_credit_note",
        object_id=credit_note.credit_note_number,
        object_repr=str(credit_note),
        reason=f"Created supplier credit note {credit_note.credit_note_number} ({credit_note.currency} {credit_note.amount})",
        metadata={
            "supplier_id": supplier.pk,
            "amount": str(credit_note.amount),
            "reason": reason_str,
        },
    )
    return credit_note


def apply_credit_note_to_invoice(
    credit_note: SupplierCreditNote,
    invoice: SupplierInvoice,
    amount: Decimal,
    notes: str = "",
    user: Any = None,
) -> CreditNoteApplication:
    """Apply an unapplied credit note amount against an approved supplier invoice balance."""
    amount_dec = Decimal(str(amount))
    if amount_dec <= Decimal("0.00"):
        raise ValidationError(_("Applied credit amount must be greater than zero."))

    if credit_note.supplier_id != invoice.supplier_id:
        raise ValidationError(_("Credit note and invoice belong to different suppliers."))

    if credit_note.status != CreditNoteStatus.APPROVED:
        raise CreditNoteApplicationError(f"Cannot apply credit note in '{credit_note.status}' status.")

    if invoice.status not in (SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID):
        raise InvalidInvoiceOperationError(
            f"Cannot apply credit note to invoice in '{invoice.status}' status. Only approved or partially paid invoices can be credited."
        )

    with transaction.atomic():
        cn = SupplierCreditNote.objects.select_for_update().get(pk=credit_note.pk)
        inv = SupplierInvoice.objects.select_for_update().get(pk=invoice.pk)

        if amount_dec > cn.unapplied_amount:
            raise CreditNoteApplicationError(
                f"Applied amount {amount_dec} exceeds available credit balance {cn.unapplied_amount}."
            )

        if amount_dec > inv.outstanding_balance:
            raise InsufficientInvoiceBalanceError(
                f"Applied amount {amount_dec} exceeds invoice outstanding balance {inv.outstanding_balance}."
            )

        app = CreditNoteApplication.objects.create(
            credit_note=cn,
            invoice=inv,
            amount=amount_dec,
            applied_by=user,
        )

        cn.amount_applied += amount_dec
        if cn.unapplied_amount == Decimal("0.00"):
            cn.status = CreditNoteStatus.APPLIED
        cn.save(update_fields=["amount_applied", "status", "updated_at"])

        inv.amount_credited += amount_dec
        if inv.outstanding_balance == Decimal("0.00"):
            inv.status = SupplierInvoiceStatus.PAID
        else:
            inv.status = SupplierInvoiceStatus.PARTIALLY_PAID
        inv.save(update_fields=["amount_credited", "status", "updated_at"])

    audit_service.record(
        actor=user,
        domain=AuditEvent.Domain.PAYMENTS,
        action="credit_note.apply",
        object_type="procurement.credit_note_application",
        object_id=str(app.pk),
        object_repr=str(app),
        reason=f"Applied credit {cn.credit_note_number} ({amount_dec}) to invoice {inv.invoice_number}",
        metadata={
            "credit_note_id": cn.pk,
            "invoice_id": inv.pk,
            "amount": str(amount_dec),
        },
    )
    return app

