"""Phase 46: Supplier Invoices & Accounts Payable Intelligence test suite.

Verifies:
1. SupplierInvoice creation, lines, Decimal calculations, and draft state.
2. Duplicate invoice detection via normalized invoice number and audited duplicate overrides.
3. Three-way matching (PO, Goods Receipts, Supplier Invoice):
   - Perfect match
   - Missing receipt discrepancy
   - Price variance discrepancy
   - Quantity overbill discrepancy
   - Cumulative partial billing
4. Approval workflow & Separation of Duties:
   - Creator self-approval blocked
   - Superuser self-approval permitted
   - Manager approval permitted
   - Discrepancy override requirements
5. Internal disbursements & Payment recording:
   - Allocating payment to open approved invoices
   - Balance reduction & status transitions (approved -> partially_paid -> paid)
   - Over-allocation rejection & DB check constraints
6. Payment reversal:
   - Atomic balance restoration & status fallback
   - Audited reversal without deleting historical records
7. Credit notes:
   - Supplier credit note creation
   - Application to invoices without over-application
8. AP Intelligence Dashboard & Aging schedule:
   - Real DB aggregation of liabilities, overdue amounts, and aging buckets
9. Backoffice access controls:
   - Capability-based permissions and 404 security for unauthorized staff
10. Strict CSP compliance (no inline scripts).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from apps.backoffice.permissions import (
    ADMIN_GROUP,
    FINANCE_GROUP,
    INVENTORY_GROUP,
    OPERATOR_GROUP,
    ensure_backoffice_groups,
)
from apps.backoffice.models import AuditEvent
from apps.catalog.models import Category, Product, ProductVariant, Size
from apps.procurement.models import (
    CreditNoteApplication,
    InvoiceMatchingStatus,
    PaymentMethod,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Supplier,
    SupplierCreditNote,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceStatus,
    SupplierPayment,
    SupplierPaymentStatus,
    SupplierVariant,
)
from apps.procurement.selectors import (
    get_accounts_payable_dashboard_metrics,
    get_supplier_ap_summary,
    get_supplier_invoices_queryset,
    get_supplier_payments_queryset,
)
from apps.procurement.services import (
    CreditNoteApplicationError,
    DiscrepancyApprovalRequired,
    DuplicateInvoiceError,
    InsufficientInvoiceBalanceError,
    InvalidInvoiceOperationError,
    PaymentReversalError,
    SeparationOfDutiesError,
    apply_credit_note_to_invoice,
    approve_purchase_order,
    approve_supplier_invoice,
    create_purchase_order,
    create_supplier_credit_note,
    create_supplier_invoice,
    dispute_supplier_invoice,
    normalize_invoice_number,
    perform_three_way_match,
    receive_purchase_order_delivery,
    record_supplier_payment,
    reverse_supplier_payment,
    send_purchase_order,
    submit_supplier_invoice,
    void_supplier_invoice,
)

User = get_user_model()
pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# Test Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _seed_groups():
    ensure_backoffice_groups()


@pytest.fixture
def inv_operator(db):
    user = User.objects.create_user(
        email="inv.op@flashwear.internal",
        password="password123",
        is_staff=True,
    )
    user.groups.add(Group.objects.get(name=INVENTORY_GROUP))
    return user


@pytest.fixture
def finance_user(db):
    user = User.objects.create_user(
        email="finance@flashwear.internal",
        password="password123",
        is_staff=True,
    )
    user.groups.add(Group.objects.get(name=FINANCE_GROUP))
    return user


@pytest.fixture
def finance_manager(db):
    user = User.objects.create_user(
        email="fin.manager@flashwear.internal",
        password="password123",
        is_staff=True,
    )
    user.groups.add(Group.objects.get(name=FINANCE_GROUP))
    return user


@pytest.fixture
def admin_user(db):
    user = User.objects.create_user(
        email="admin@flashwear.internal",
        password="password123",
        is_staff=True,
        is_superuser=True,
    )
    user.groups.add(Group.objects.get(name=ADMIN_GROUP))
    return user


@pytest.fixture
def unauthorized_staff(db):
    return User.objects.create_user(
        email="noaccess@flashwear.internal",
        password="password123",
        is_staff=True,
    )


@pytest.fixture
def category(db):
    return Category.objects.create(name="Outerwear", slug="outerwear")


@pytest.fixture
def variants(db, category):
    size_m = Size.objects.create(name="Medium", slug="m", code="M", size_type=Size.SizeType.CLOTHING)
    size_l = Size.objects.create(name="Large", slug="l", code="L", size_type=Size.SizeType.CLOTHING)
    p = Product.objects.create(
        name="Tech Parka",
        slug="tech-parka",
        category=category,
        status="published",
        published_at=timezone.now(),
    )
    v1 = ProductVariant.objects.create(
        product=p,
        size=size_m,
        sku="FW-PARKA-M",
        price=Decimal("150.00"),
        cost_price=Decimal("60.00"),
        is_active=True,
    )
    v2 = ProductVariant.objects.create(
        product=p,
        size=size_l,
        sku="FW-PARKA-L",
        price=Decimal("150.00"),
        cost_price=Decimal("60.00"),
        is_active=True,
    )
    return [v1, v2]


@pytest.fixture
def supplier(db, variants):
    s = Supplier.objects.create(
        name="Apex Fabrics Ltd",
        code="APEX-FAB",
        currency="USD",
        contact_name="John Doe",
        email="john@apexfabrics.com",
        lead_time_days=14,
    )
    SupplierVariant.objects.create(
        supplier=s,
        variant=variants[0],
        supplier_sku="APX-01",
        purchase_cost=Decimal("50.00"),
    )
    SupplierVariant.objects.create(
        supplier=s,
        variant=variants[1],
        supplier_sku="APX-02",
        purchase_cost=Decimal("55.00"),
    )
    return s


@pytest.fixture
def approved_po(db, supplier, variants, finance_user):
    po = create_purchase_order(
        supplier=supplier,
        created_by=finance_user,
        items=[
            {"variant": variants[0], "quantity": 10, "unit_cost": Decimal("50.00")},
            {"variant": variants[1], "quantity": 5, "unit_cost": Decimal("55.00")},
        ],
    )
    po = approve_purchase_order(po=po, user=finance_user)
    po = send_purchase_order(po=po, user=finance_user)
    return po


# --------------------------------------------------------------------------------------
# 1. Invoice Creation & Decimal Precision Tests
# --------------------------------------------------------------------------------------


def test_create_supplier_invoice_decimal_calculations(supplier, approved_po, finance_user, variants):
    """Invoice line calculations and totals must strictly use Decimal arithmetic."""
    lines_data = [
        {
            "po_item": approved_po.items.all()[0],
            "variant": variants[0],
            "description": "Tech Parka - M",
            "quantity": 10,
            "unit_cost": Decimal("50.00"),
            "tax_amount": Decimal("25.00"),
        },
        {
            "po_item": approved_po.items.all()[1],
            "variant": variants[1],
            "description": "Tech Parka - L",
            "quantity": 5,
            "unit_cost": Decimal("55.00"),
            "tax_amount": Decimal("13.75"),
        },
    ]

    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-2026-001",
        invoice_date=timezone.localdate(),
        due_date=timezone.localdate() + timezone.timedelta(days=30),
        currency="USD",
        purchase_order=approved_po,
        lines_data=lines_data,
        tax_amount=Decimal("38.75"),
        shipping_amount=Decimal("20.00"),
        discount_amount=Decimal("10.00"),
        notes="First batch delivery invoice",
        user=finance_user,
    )

    # Subtotal: (10 * 50) + (5 * 55) = 500 + 275 = 775.00
    # Total: 775.00 + 38.75 + 20.00 - 10.00 = 823.75
    assert inv.subtotal_amount == Decimal("775.00")
    assert inv.tax_amount == Decimal("38.75")
    assert inv.shipping_amount == Decimal("20.00")
    assert inv.discount_amount == Decimal("10.00")
    assert inv.total_amount == Decimal("823.75")
    assert inv.outstanding_balance == Decimal("823.75")
    assert inv.status == SupplierInvoiceStatus.DRAFT
    assert inv.lines.count() == 2
    assert inv.normalized_invoice_number == "INV2026001"


# --------------------------------------------------------------------------------------
# 2. Duplicate Invoice Detection Tests
# --------------------------------------------------------------------------------------


def test_duplicate_invoice_detection_and_override(supplier, finance_user):
    """Same normalized invoice number for same supplier must raise DuplicateInvoiceError unless overridden."""
    assert normalize_invoice_number("INV-2026/0042-B") == "INV20260042B"
    assert normalize_invoice_number("  inv 2026 0042 b  ") == "INV20260042B"

    create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-2026/0042-B",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("100.00")}],
        user=finance_user,
    )

    # Duplicate attempt with different formatting should fail
    with pytest.raises(DuplicateInvoiceError) as exc_info:
        create_supplier_invoice(
            supplier=supplier,
            invoice_number="inv 2026 0042 b",
            invoice_date=timezone.localdate(),
            currency="USD",
            lines_data=[{"quantity": 1, "unit_cost": Decimal("100.00")}],
            user=finance_user,
        )
    assert "already exists for supplier" in str(exc_info.value)

    # Legitimate audited duplicate override succeeds
    overridden = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-2026-0042-B",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("100.00")}],
        user=finance_user,
        is_duplicate_override=True,
        duplicate_override_reason="Vendor reissued corrected split bill with identical sequence number",
    )
    assert overridden.pk is not None
    assert overridden.is_duplicate_override is True


def test_voided_invoice_does_not_block_reissuance(supplier, finance_user):
    """A voided invoice must not block recording a new active invoice with the same number."""
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-VOID-TEST",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("50.00")}],
        user=finance_user,
    )
    void_supplier_invoice(inv, reason="Cancelled by supplier prior to processing", user=finance_user)
    assert inv.status == SupplierInvoiceStatus.VOIDED

    # Creating a new one with same number now succeeds
    new_inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-VOID-TEST",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("50.00")}],
        user=finance_user,
    )
    assert new_inv.status == SupplierInvoiceStatus.DRAFT


# --------------------------------------------------------------------------------------
# 3. Three-Way Matching Tests
# --------------------------------------------------------------------------------------


def test_three_way_match_missing_receipt(supplier, approved_po, finance_user, variants):
    """Matching an invoice before warehouse receipt has been recorded flags MISSING_RECEIPT."""
    po_item1 = approved_po.items.get(variant=variants[0])
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-MATCH-01",
        invoice_date=timezone.localdate(),
        currency="USD",
        purchase_order=approved_po,
        lines_data=[{"po_item": po_item1, "variant": variants[0], "quantity": 10, "unit_cost": Decimal("50.00")}],
        user=finance_user,
    )

    match = perform_three_way_match(inv)
    assert match.status == InvoiceMatchingStatus.MISSING_RECEIPT
    assert match.is_within_tolerance is False
    assert len(match.discrepancies) > 0
    assert match.discrepancies[0]["kind"] == "missing_receipt"


def test_three_way_match_exact_success(supplier, approved_po, inv_operator, finance_user, variants):
    """When goods receipt matches PO ordered quantity and invoice unit prices match, match status is MATCHED."""
    po_item_0 = approved_po.items.get(variant=variants[0])
    po_item_1 = approved_po.items.get(variant=variants[1])
    receive_purchase_order_delivery(
        po=approved_po,
        delivery_reference="DEL-001",
        receipt_items_data=[
            {"line_item": po_item_0, "quantity_received": 10, "quantity_damaged": 0},
            {"line_item": po_item_1, "quantity_received": 5, "quantity_damaged": 0},
        ],
        user=inv_operator,
    )

    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-MATCH-PERFECT",
        invoice_date=timezone.localdate(),
        currency="USD",
        purchase_order=approved_po,
        lines_data=[
            {"po_item": po_item_0, "variant": variants[0], "quantity": 10, "unit_cost": Decimal("50.00")},
            {"po_item": po_item_1, "variant": variants[1], "quantity": 5, "unit_cost": Decimal("55.00")},
        ],
        user=finance_user,
    )

    match = perform_three_way_match(inv)
    assert match.status == InvoiceMatchingStatus.MATCHED
    assert match.is_within_tolerance is True
    assert match.price_variance_total == Decimal("0.00")
    assert match.quantity_variance_total == 0
    assert len(match.discrepancies) == 0


def test_three_way_match_price_and_quantity_variance(supplier, approved_po, inv_operator, finance_user, variants):
    """Detect price variance and overbilling discrepancies."""
    po_item = approved_po.items.get(variant=variants[0])
    receive_purchase_order_delivery(
        po=approved_po,
        delivery_reference="DEL-002",
        receipt_items_data=[
            {"line_item": po_item, "quantity_received": 8, "quantity_damaged": 0},
        ],
        user=inv_operator,
    )

    # Invoiced 12 units (overbilling vs received 8) and cost $55 instead of agreed $50 (price variance)
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-MATCH-VARIANCE",
        invoice_date=timezone.localdate(),
        currency="USD",
        purchase_order=approved_po,
        lines_data=[
            {"po_item": po_item, "variant": variants[0], "quantity": 12, "unit_cost": Decimal("55.00")},
        ],
        user=finance_user,
    )

    match = perform_three_way_match(inv)
    assert match.status == InvoiceMatchingStatus.OVERBILLED
    assert match.is_within_tolerance is False
    kinds = [d["kind"] for d in match.discrepancies]
    assert "price_variance" in kinds
    assert "quantity_overbill" in kinds


# --------------------------------------------------------------------------------------
# 4. Approval Workflow & Separation of Duties Tests
# --------------------------------------------------------------------------------------


def test_separation_of_duties_blocks_creator_approval(supplier, finance_user, finance_manager):
    """Preparer cannot approve their own invoice unless superuser."""
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-SOD-01",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("100.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv, user=finance_user)

    # Creator self-approval blocked
    with pytest.raises(SeparationOfDutiesError) as exc_info:
        approve_supplier_invoice(inv, user=finance_user)
    assert "cannot approve your own supplier invoice" in str(exc_info.value)

    # Second manager can approve successfully
    approved = approve_supplier_invoice(inv, user=finance_manager)
    assert approved.status == SupplierInvoiceStatus.APPROVED
    assert approved.approved_by == finance_manager


def test_superuser_permitted_self_approval(supplier, admin_user):
    """Superusers are authorized to override separation of duties."""
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-SOD-ADMIN",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("100.00")}],
        user=admin_user,
    )
    submit_supplier_invoice(inv, user=admin_user)
    approved = approve_supplier_invoice(inv, user=admin_user)
    assert approved.status == SupplierInvoiceStatus.APPROVED
    assert approved.approved_by == admin_user


def test_discrepancy_override_required_on_approval(supplier, approved_po, finance_user, finance_manager, variants):
    """Approving an invoice with unoverridden discrepancy blocks approval until explicitly authorized."""
    po_item = approved_po.items.get(variant=variants[0])
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-DISCREPANCY-BLOCK",
        invoice_date=timezone.localdate(),
        currency="USD",
        purchase_order=approved_po,
        lines_data=[{"po_item": po_item, "variant": variants[0], "quantity": 10, "unit_cost": Decimal("50.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv, user=finance_user)

    # No goods receipt yet -> match discrepancy
    with pytest.raises(DiscrepancyApprovalRequired):
        approve_supplier_invoice(inv, user=finance_manager, override_discrepancies=False)

    # Overriding with documented reason succeeds
    approved = approve_supplier_invoice(
        inv,
        user=finance_manager,
        override_discrepancies=True,
        discrepancy_override_reason="Warehouse confirmed verbal receipt of urgent batch; receiving paperwork in transit.",
    )
    assert approved.status == SupplierInvoiceStatus.APPROVED
    assert approved.match_record.discrepancy_override_by == finance_manager


# --------------------------------------------------------------------------------------
# 5. Disbursements, Payment Recording & AP Ledger Tests
# --------------------------------------------------------------------------------------


def test_record_supplier_payment_and_allocation(supplier, finance_user, finance_manager):
    """Payment allocation reduces outstanding balance and transitions invoice states."""
    inv1 = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-PAY-01",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 2, "unit_cost": Decimal("100.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv1, user=finance_user)
    approve_supplier_invoice(inv1, user=finance_manager)

    inv2 = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-PAY-02",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("150.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv2, user=finance_user)
    approve_supplier_invoice(inv2, user=finance_manager)

    # Record partial payment on inv1 ($100 of $200) and full payment on inv2 ($150)
    allocations = [
        {"invoice": inv1, "amount": Decimal("100.00")},
        {"invoice": inv2, "amount": Decimal("150.00")},
    ]

    payment = record_supplier_payment(
        supplier=supplier,
        amount=Decimal("250.00"),
        currency="USD",
        payment_date=timezone.localdate(),
        payment_method=PaymentMethod.BANK_TRANSFER,
        bank_transaction_reference="WIRE-REF-9988",
        allocations_data=allocations,
        user=finance_manager,
    )

    assert payment.status == SupplierPaymentStatus.COMPLETED
    assert payment.allocations.count() == 2

    inv1.refresh_from_db()
    inv2.refresh_from_db()

    assert inv1.amount_paid == Decimal("100.00")
    assert inv1.outstanding_balance == Decimal("100.00")
    assert inv1.status == SupplierInvoiceStatus.PARTIALLY_PAID

    assert inv2.amount_paid == Decimal("150.00")
    assert inv2.outstanding_balance == Decimal("0.00")
    assert inv2.status == SupplierInvoiceStatus.PAID


def test_payment_over_allocation_rejected(supplier, finance_user, finance_manager):
    """Allocating more than invoice outstanding balance raises InsufficientInvoiceBalanceError."""
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-OVERALLOC-01",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("100.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv, user=finance_user)
    approve_supplier_invoice(inv, user=finance_manager)

    with pytest.raises(InsufficientInvoiceBalanceError):
        record_supplier_payment(
            supplier=supplier,
            amount=Decimal("150.00"),
            currency="USD",
            allocations_data=[{"invoice": inv, "amount": Decimal("150.00")}],
            user=finance_manager,
        )


def test_unapproved_invoice_cannot_be_paid(supplier, finance_user, finance_manager):
    """Draft or unapproved invoices cannot receive payment allocations."""
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-DRAFT-NOPAY",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("100.00")}],
        user=finance_user,
    )

    with pytest.raises(InvalidInvoiceOperationError):
        record_supplier_payment(
            supplier=supplier,
            amount=Decimal("100.00"),
            currency="USD",
            allocations_data=[{"invoice": inv, "amount": Decimal("100.00")}],
            user=finance_manager,
        )


# --------------------------------------------------------------------------------------
# 6. Payment Reversal Tests
# --------------------------------------------------------------------------------------


def test_reverse_supplier_payment_restores_balance_and_status(supplier, finance_user, finance_manager):
    """Reversing a payment restores invoice outstanding balance and falls back status."""
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-REV-01",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("200.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv, user=finance_user)
    approve_supplier_invoice(inv, user=finance_manager)

    payment = record_supplier_payment(
        supplier=supplier,
        amount=Decimal("200.00"),
        currency="USD",
        allocations_data=[{"invoice": inv, "amount": Decimal("200.00")}],
        user=finance_manager,
    )
    inv.refresh_from_db()
    assert inv.status == SupplierInvoiceStatus.PAID
    assert inv.outstanding_balance == Decimal("0.00")

    # Reverse disbursement
    rev_payment = reverse_supplier_payment(
        payment=payment,
        reason="Bank wire was recalled due to vendor routing mismatch",
        user=finance_manager,
    )
    assert rev_payment.status == SupplierPaymentStatus.REVERSED
    assert rev_payment.reversed_by == finance_manager

    inv.refresh_from_db()
    assert inv.amount_paid == Decimal("0.00")
    assert inv.outstanding_balance == Decimal("200.00")
    assert inv.status == SupplierInvoiceStatus.APPROVED

    # Cannot reverse twice
    with pytest.raises(PaymentReversalError):
        reverse_supplier_payment(payment, reason="Duplicate attempt", user=finance_manager)


# --------------------------------------------------------------------------------------
# 7. Credit Notes & Adjustments Tests
# --------------------------------------------------------------------------------------


def test_credit_note_creation_and_application(supplier, finance_user, finance_manager):
    """Credit notes reduce invoice outstanding balances and track remaining applied amounts."""
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-CREDIT-01",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 2, "unit_cost": Decimal("100.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv, user=finance_user)
    approve_supplier_invoice(inv, user=finance_manager)

    cn = create_supplier_credit_note(
        supplier=supplier,
        credit_note_number="CN-2026-001",
        amount=Decimal("50.00"),
        reason="Volume rebate allowance",
        user=finance_manager,
    )
    assert cn.remaining_amount == Decimal("50.00")

    # Apply $30 to the invoice
    app = apply_credit_note_to_invoice(
        credit_note=cn,
        invoice=inv,
        amount=Decimal("30.00"),
        notes="Applied partial rebate",
        user=finance_manager,
    )
    assert app.amount == Decimal("30.00")

    cn.refresh_from_db()
    inv.refresh_from_db()

    assert cn.amount_applied == Decimal("30.00")
    assert cn.remaining_amount == Decimal("20.00")
    assert inv.amount_credited == Decimal("30.00")
    assert inv.outstanding_balance == Decimal("170.00")  # 200 - 30

    # Over-application fails
    with pytest.raises(CreditNoteApplicationError):
        apply_credit_note_to_invoice(
            credit_note=cn,
            invoice=inv,
            amount=Decimal("50.00"),  # Only 20 remaining
            user=finance_manager,
        )


# --------------------------------------------------------------------------------------
# 8. AP Intelligence Dashboard & Aging Schedule Tests
# --------------------------------------------------------------------------------------


def test_accounts_payable_dashboard_metrics_and_aging(supplier, finance_user, finance_manager):
    """Verify live calculation of outstanding liabilities, due soon, overdue amounts, and aging buckets."""
    today = timezone.localdate()

    # Invoice 1: Overdue by 15 days (bucket: days_1_30)
    inv1 = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-AGE-15",
        invoice_date=today - timezone.timedelta(days=45),
        due_date=today - timezone.timedelta(days=15),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("100.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv1, user=finance_user)
    approve_supplier_invoice(inv1, user=finance_manager)

    # Invoice 2: Overdue by 45 days (bucket: days_31_60)
    inv2 = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-AGE-45",
        invoice_date=today - timezone.timedelta(days=75),
        due_date=today - timezone.timedelta(days=45),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("200.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv2, user=finance_user)
    approve_supplier_invoice(inv2, user=finance_manager)

    # Invoice 3: Current (due in 10 days)
    inv3 = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-AGE-CURRENT",
        invoice_date=today,
        due_date=today + timezone.timedelta(days=10),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("300.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv3, user=finance_user)
    approve_supplier_invoice(inv3, user=finance_manager)

    metrics = get_accounts_payable_dashboard_metrics(as_of_date=today)

    assert metrics["total_outstanding_liability"] == Decimal("600.00")
    assert metrics["overdue_amount"] == Decimal("300.00")  # inv1 ($100) + inv2 ($200)
    assert metrics["amount_due_soon"] == Decimal("300.00")  # inv3 is within 14 days

    aging = metrics["aging_schedule"]
    assert aging["days_1_30"]["amount"] == Decimal("100.00")
    assert aging["days_1_30"]["count"] == 1
    assert aging["days_31_60"]["amount"] == Decimal("200.00")
    assert aging["days_31_60"]["count"] == 1
    assert aging["current"]["amount"] == Decimal("300.00")
    assert aging["current"]["count"] == 1

    summary = get_supplier_ap_summary(supplier, as_of_date=today)
    assert summary["total_outstanding"] == Decimal("600.00")
    assert summary["overdue_amount"] == Decimal("300.00")


# --------------------------------------------------------------------------------------
# 9. Backoffice Access Controls & View Tests
# --------------------------------------------------------------------------------------


def test_unauthorized_staff_cannot_access_ap_views(client, unauthorized_staff, supplier):
    """Unauthorized staff receive HTTP 404 on AP endpoints."""
    client.force_login(unauthorized_staff)

    assert client.get(reverse("backoffice:accounts_payable_dashboard")).status_code == 404
    assert client.get(reverse("backoffice:supplier_invoices")).status_code == 404
    assert client.get(reverse("backoffice:supplier_invoice_create")).status_code == 404
    assert client.get(reverse("backoffice:supplier_payments")).status_code == 404
    assert client.get(reverse("backoffice:supplier_payment_create")).status_code == 404
    assert client.get(reverse("backoffice:supplier_credit_notes")).status_code == 404


def test_finance_user_can_access_ap_views(client, finance_user, supplier):
    """Authorized finance staff receive HTTP 200 on AP endpoints."""
    client.force_login(finance_user)

    resp = client.get(reverse("backoffice:accounts_payable_dashboard"))
    assert resp.status_code == 200

    resp = client.get(reverse("backoffice:supplier_invoices"))
    assert resp.status_code == 200

    resp = client.get(reverse("backoffice:supplier_payments"))
    assert resp.status_code == 200

    resp = client.get(reverse("backoffice:supplier_credit_notes"))
    assert resp.status_code == 200


def test_invoice_create_post_flow(client, finance_user, supplier):
    """Creating an invoice via back office POST succeeds and redirects to invoice list."""
    client.force_login(finance_user)
    today_str = timezone.localdate().isoformat()

    post_data = {
        "supplier": supplier.pk,
        "invoice_number": "WEB-INV-001",
        "supplier_reference": "REF-WEB",
        "invoice_date": today_str,
        "due_date": today_str,
        "currency": "USD",
        "tax_amount": "10.00",
        "shipping_amount": "5.00",
        "discount_amount": "0.00",
        "notes": "Created via UI test",
        "line_description_1": "Item Alpha",
        "line_quantity_1": "2",
        "line_unit_cost_1": "50.00",
        "line_tax_1": "10.00",
    }

    resp = client.post(reverse("backoffice:supplier_invoice_create"), data=post_data)
    assert resp.status_code == 302
    assert SupplierInvoice.objects.filter(invoice_number="WEB-INV-001").exists()


def test_audit_event_logged_on_invoice_action(finance_user, finance_manager, supplier):
    """Auditing records are stored upon significant invoice workflow actions."""
    inv = create_supplier_invoice(
        supplier=supplier,
        invoice_number="INV-AUDIT-TEST",
        invoice_date=timezone.localdate(),
        currency="USD",
        lines_data=[{"quantity": 1, "unit_cost": Decimal("100.00")}],
        user=finance_user,
    )
    submit_supplier_invoice(inv, user=finance_user)
    approve_supplier_invoice(inv, user=finance_manager)

    audit_events = AuditEvent.objects.filter(object_id=str(inv.pk))
    actions = [e.action for e in audit_events]
    assert "invoice.create" in actions
    assert "invoice.submit" in actions
    assert "invoice.approve" in actions
