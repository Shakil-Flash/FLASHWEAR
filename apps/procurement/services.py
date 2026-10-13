"""Phase 45: Procurement & Purchase Order domain services.

Authoritative operations for:
1. Supplier profile creation and maintenance.
2. Sourcing matrix (SupplierVariant) configuration.
3. Purchase Order lifecycle transitions (draft -> pending_approval -> approved -> ordered -> received / cancelled).
4. Atomic goods receiving integrated with apps.inventory.services.adjust_stock and the immutable InventoryMovement ledger.
5. Strict validations: over-receipt prevention, idempotency, Decimal precision, and audit logging.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.backoffice.models import AuditEvent
from apps.backoffice.services import audit as audit_service
from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryMovement
from apps.inventory.services import adjust_stock
from apps.procurement.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptItem,
    PurchaseOrderStatus,
    Supplier,
    SupplierVariant,
)

__all__ = [
    "DuplicateReceiptError",
    "InvalidStatusTransition",
    "OrderImmutableError",
    "OverReceiptError",
    "ProcurementError",
    "approve_purchase_order",
    "cancel_purchase_order",
    "create_purchase_order",
    "create_supplier",
    "generate_po_reference",
    "receive_purchase_order_delivery",
    "send_purchase_order",
    "submit_for_approval",
    "update_purchase_order",
    "update_supplier",
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
    items: list[dict[str, Any]],
    user,
    notes: str = "",
    idempotency_key: str = "",
) -> PurchaseOrderReceipt:
    """Receive a physical shipment against an active purchase order.

    Critical Invariants:
    1. PO must be in ORDERED, PARTIALLY_RECEIVED, or APPROVED status.
    2. Atomic execution: Stock.on_hand incremented via authoritative adjust_stock(kind=RECEIVED).
    3. Over-receipt protection: accepted + damaged units cannot exceed line item outstanding quantity.
    4. Damaged/quarantined units are recorded in receipt item history but NEVER credited to sellable stock.
    5. Idempotent: duplicate submissions with the same delivery_reference for this PO are rejected.
    6. Lock order: PO locked first, line items second, Stock counters locked last via adjust_stock.
    7. PO status automatically transitions to PARTIALLY_RECEIVED or FULLY_RECEIVED based on verified quantities.

    Args:
        po: PurchaseOrder instance.
        delivery_reference: Carrier tracking number or supplier delivery note (required, unique per PO).
        items: List of dicts: ``[{"line_item_id": int, "quantity_received": int, "quantity_damaged": int, "notes": str}, ...]``
        user: Receiving staff member.
        notes: General receiving inspection notes.
        idempotency_key: Optional external idempotency token.
    """
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
        line_item_id = entry.get("line_item_id")
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
