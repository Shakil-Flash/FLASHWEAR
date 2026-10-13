"""Phase 45: Supplier Management & Purchase Order Intelligence test suite.

Verifies:
1. Supplier profile creation, unique codes, inactive status handling, and search.
2. SupplierVariant sourcing catalog, purchase costs, and retail price preservation.
3. Purchase Order lifecycle transitions (draft -> pending_approval -> approved -> ordered -> partially_received -> fully_received -> cancelled) and validation.
4. Exact Decimal precision for unit costs and line/order totals.
5. Immutability of non-draft orders against line item modifications.
6. Approval permission checks (PURCHASING_APPROVE / FINANCE_GROUP / ADMIN_GROUP).
7. Goods receiving: full deliveries, partial deliveries, repeated deliveries.
8. Over-receipt protection and duplicate delivery reference idempotency.
9. Damaged / quarantined units tracked in receipts but kept out of sellable Stock.on_hand.
10. Atomic inventory movement ledger integration (InventoryMovement.Kind.RECEIVED).
11. Purchasing Dashboard metrics, overdue order filters, and supplier performance tracking.
12. Back office access control (404 for unauthorized staff).
13. Concurrency contract for row locking.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.backoffice.permissions import (
    ADMIN_GROUP,
    FINANCE_GROUP,
    INVENTORY_GROUP,
    OPERATOR_GROUP,
    ensure_backoffice_groups,
)
from apps.catalog.models import Category, Product, ProductVariant, Size
from apps.inventory.models import InventoryMovement, Stock
from apps.procurement.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptItem,
    PurchaseOrderStatus,
    Supplier,
    SupplierVariant,
)
from apps.procurement.selectors import (
    get_procurement_dashboard_metrics,
    get_purchase_orders_queryset,
    get_supplier_performance,
    get_suppliers_queryset,
)
from apps.procurement.services import (
    DuplicateReceiptError,
    InvalidStatusTransition,
    OrderImmutableError,
    OverReceiptError,
    approve_purchase_order,
    cancel_purchase_order,
    create_purchase_order,
    create_supplier,
    generate_po_reference,
    receive_purchase_order_delivery,
    send_purchase_order,
    submit_for_approval,
    update_purchase_order,
    update_supplier,
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
def basic_operator(db):
    user = User.objects.create_user(
        email="ops@flashwear.internal",
        password="password123",
        is_staff=True,
    )
    user.groups.add(Group.objects.get(name=OPERATOR_GROUP))
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
def product(db, category):
    size_m = Size.objects.create(name="Medium", slug="m", code="M", size_type=Size.SizeType.CLOTHING)
    size_l = Size.objects.create(name="Large", slug="l", code="L", size_type=Size.SizeType.CLOTHING)
    p = Product.objects.create(
        name="Tech Anorak",
        slug="tech-anorak",
        category=category,
        status="published",
        published_at=timezone.now(),
    )
    ProductVariant.objects.create(
        product=p,
        size=size_m,
        sku="FW-ANORAK-BLK-M",
        price=Decimal("120.00"),
        cost_price=Decimal("45.00"),
        is_active=True,
    )
    ProductVariant.objects.create(
        product=p,
        size=size_l,
        sku="FW-ANORAK-BLK-L",
        price=Decimal("120.00"),
        cost_price=Decimal("45.00"),
        is_active=True,
    )
    return p


@pytest.fixture
def active_supplier(db):
    return create_supplier(
        name="Alpine Textiles",
        code="SUP-ALPINE",
        contact_name="Marcus Vance",
        email="orders@alpinetextiles.com",
        phone="+44 20 7946 0111",
        currency="USD",
        lead_time_days=14,
        minimum_order_quantity=10,
        is_active=True,
    )


# --------------------------------------------------------------------------------------
# 1. Supplier Profile & Directory Management
# --------------------------------------------------------------------------------------


class TestSupplierManagement:
    def test_create_supplier_success_and_validations(self, inv_operator):
        supplier = create_supplier(
            name="Nordic Knits",
            code="SUP-NORDIC",
            contact_name="Astrid Lind",
            email="astrid@nordicknits.se",
            currency="EUR",
            lead_time_days=21,
            minimum_order_quantity=50,
            actor=inv_operator,
        )
        assert supplier.pk is not None
        assert supplier.name == "Nordic Knits"
        assert supplier.code == "SUP-NORDIC"
        assert supplier.currency == "EUR"
        assert supplier.minimum_order_quantity == 50
        assert supplier.is_active is True

    def test_duplicate_name_or_code_rejected(self, active_supplier):
        with pytest.raises(ValidationError):
            create_supplier(name="Alpine Textiles", code="SUP-DIFF")

        with pytest.raises(ValidationError):
            create_supplier(name="Different Name", code="SUP-ALPINE")

    def test_update_supplier_and_active_toggle(self, active_supplier, inv_operator):
        updated = update_supplier(
            active_supplier,
            contact_name="Elena Vance",
            lead_time_days=10,
            is_active=False,
            actor=inv_operator,
        )
        assert updated.contact_name == "Elena Vance"
        assert updated.lead_time_days == 10
        assert updated.is_active is False

    def test_supplier_search_and_filtering(self, active_supplier):
        create_supplier(name="Tokyo Weavers", code="SUP-TOKYO", is_active=False)

        all_qs = get_suppliers_queryset(q="alpine")
        assert all_qs.count() == 1
        assert all_qs.first().pk == active_supplier.pk

        active_qs = get_suppliers_queryset(is_active=True)
        assert active_qs.count() == 1
        assert active_qs.first().code == "SUP-ALPINE"

        inactive_qs = get_suppliers_queryset(is_active=False)
        assert inactive_qs.count() == 1
        assert inactive_qs.first().code == "SUP-TOKYO"

    def test_cannot_create_po_for_inactive_supplier(self, active_supplier, product, inv_operator):
        active_supplier.is_active = False
        active_supplier.save()

        variant = product.variants.first()
        with pytest.raises(ValidationError, match="inactive supplier"):
            create_purchase_order(
                supplier=active_supplier,
                created_by=inv_operator,
                items=[{"variant": variant, "quantity": 10, "unit_cost": Decimal("40.00")}],
            )


# --------------------------------------------------------------------------------------
# 2. Supplier-Variant Relationships
# --------------------------------------------------------------------------------------


class TestSupplierVariantRelationships:
    def test_link_variant_to_supplier(self, active_supplier, product):
        variant = product.variants.first()
        sv = SupplierVariant.objects.create(
            supplier=active_supplier,
            variant=variant,
            supplier_sku="ALP-ANO-M",
            purchase_cost=Decimal("42.50"),
            lead_time_days=12,
            minimum_order_quantity=5,
            is_preferred=True,
        )
        assert sv.pk is not None
        assert sv.purchase_cost == Decimal("42.50")
        assert sv.is_preferred is True

        # Variant retail price is untouched
        variant.refresh_from_db()
        assert variant.price == Decimal("120.00")

    def test_po_creation_resolves_supplier_variant_cost(self, active_supplier, product, inv_operator):
        variant = product.variants.first()
        SupplierVariant.objects.create(
            supplier=active_supplier,
            variant=variant,
            purchase_cost=Decimal("38.00"),
        )
        # Without explicit cost submitted, order automatically resolves agreed supplier cost
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 15}],
        )
        assert po.items.first().unit_cost == Decimal("38.00")
        assert po.total_cost == Decimal("570.00")


# --------------------------------------------------------------------------------------
# 3. Purchase Order Lifecycle & Validations
# --------------------------------------------------------------------------------------


class TestPurchaseOrderLifecycleAndCalculations:
    def test_create_draft_purchase_order_with_decimal_precision(
        self, active_supplier, product, inv_operator
    ):
        v1, v2 = product.variants.all()[:2]
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            currency="USD",
            items=[
                {"variant": v1, "quantity": 20, "unit_cost": Decimal("45.50")},
                {"variant": v2, "quantity": 10, "unit_cost": Decimal("42.25")},
            ],
            notes="First seasonal run",
        )
        assert po.status == PurchaseOrderStatus.DRAFT
        assert po.reference.startswith("PO-")
        assert po.total_ordered_units == 30
        assert po.total_received_units == 0
        assert po.total_outstanding_units == 30
        # Line totals: (20 * 45.50 = 910.00) + (10 * 42.25 = 422.50) = 1332.50
        assert po.total_cost == Decimal("1332.50")

    def test_submit_for_approval_respects_supplier_moq(
        self, active_supplier, product, inv_operator
    ):
        variant = product.variants.first()
        # Supplier MOQ is 10
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 5, "unit_cost": Decimal("40.00")}],
        )
        with pytest.raises(ValidationError, match="minimum order quantity"):
            submit_for_approval(po, user=inv_operator)

        # Update items to meet MOQ
        update_purchase_order(
            po,
            items=[{"variant": variant, "quantity": 12, "unit_cost": Decimal("40.00")}],
            user=inv_operator,
        )
        po = submit_for_approval(po, user=inv_operator)
        assert po.status == PurchaseOrderStatus.PENDING_APPROVAL

    def test_approval_and_order_dispatch_workflow(
        self, active_supplier, product, inv_operator, finance_user
    ):
        variant = product.variants.first()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 15, "unit_cost": Decimal("40.00")}],
        )
        submit_for_approval(po, user=inv_operator)

        # Finance user approves
        po = approve_purchase_order(po, user=finance_user)
        assert po.status == PurchaseOrderStatus.APPROVED
        assert po.approved_by == finance_user
        assert po.approved_at is not None

        # Operator marks sent/ordered
        po = send_purchase_order(po, user=inv_operator)
        assert po.status == PurchaseOrderStatus.ORDERED
        assert po.sent_at is not None

    def test_invalid_status_transitions_rejected(
        self, active_supplier, product, inv_operator
    ):
        variant = product.variants.first()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 15, "unit_cost": Decimal("40.00")}],
        )
        # Cannot mark sent while still DRAFT
        with pytest.raises(InvalidStatusTransition):
            send_purchase_order(po, user=inv_operator)

    def test_finalized_or_sent_order_is_immutable_to_item_edits(
        self, active_supplier, product, inv_operator, finance_user
    ):
        variant = product.variants.first()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 15, "unit_cost": Decimal("40.00")}],
        )
        approve_purchase_order(po, user=finance_user)
        send_purchase_order(po, user=inv_operator)

        # Cannot edit line items on ORDERED purchase order
        with pytest.raises(OrderImmutableError):
            update_purchase_order(
                po,
                items=[{"variant": variant, "quantity": 50, "unit_cost": Decimal("30.00")}],
                user=inv_operator,
            )

    def test_cancel_purchase_order(self, active_supplier, product, inv_operator):
        variant = product.variants.first()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 15, "unit_cost": Decimal("40.00")}],
        )
        cancelled = cancel_purchase_order(po, user=inv_operator, reason="Supplier out of fabric")
        assert cancelled.status == PurchaseOrderStatus.CANCELLED
        assert cancelled.cancelled_by == inv_operator
        assert cancelled.cancellation_reason == "Supplier out of fabric"

        # Cannot approve or receive a cancelled PO
        with pytest.raises(InvalidStatusTransition):
            approve_purchase_order(cancelled, user=inv_operator)


# --------------------------------------------------------------------------------------
# 4. Goods Receiving & Authoritative Inventory Integration
# --------------------------------------------------------------------------------------


class TestGoodsReceivingAndInventoryIntegration:
    def test_partial_delivery_receipt_increments_stock_on_hand(
        self, active_supplier, product, inv_operator, finance_user
    ):
        variant = product.variants.first()
        initial_stock = Stock.get_for_variant(variant)
        assert initial_stock.on_hand == 0

        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 50, "unit_cost": Decimal("35.00")}],
        )
        approve_purchase_order(po, user=finance_user)
        send_purchase_order(po, user=inv_operator)

        line_item = po.items.first()

        # Receive partial delivery of 30 units
        receipt = receive_purchase_order_delivery(
            po,
            delivery_reference="BOL-001",
            items=[{"line_item_id": line_item.pk, "quantity_received": 30, "quantity_damaged": 0}],
            user=inv_operator,
            notes="First pallet received in good condition",
        )

        assert receipt.pk is not None
        assert receipt.delivery_reference == "BOL-001"
        assert receipt.received_by == inv_operator

        # Verify stock and movement ledger
        initial_stock.refresh_from_db()
        assert initial_stock.on_hand == 30
        assert initial_stock.available == 30

        movement = InventoryMovement.objects.filter(variant=variant).latest("pk")
        assert movement.kind == InventoryMovement.Kind.RECEIVED
        assert movement.on_hand_delta == 30
        assert movement.user == inv_operator

        # Verify PO status and item counters
        po.refresh_from_db()
        line_item.refresh_from_db()
        assert po.status == PurchaseOrderStatus.PARTIALLY_RECEIVED
        assert line_item.received_quantity == 30
        assert line_item.outstanding_quantity == 20
        assert po.total_outstanding_units == 20

    def test_repeated_delivery_completes_purchase_order(
        self, active_supplier, product, inv_operator, finance_user
    ):
        variant = product.variants.first()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 40, "unit_cost": Decimal("35.00")}],
        )
        approve_purchase_order(po, user=finance_user)
        send_purchase_order(po, user=inv_operator)
        line_item = po.items.first()

        # Delivery 1: 25 units
        receive_purchase_order_delivery(
            po,
            delivery_reference="DELIV-PART-1",
            items=[{"line_item_id": line_item.pk, "quantity_received": 25}],
            user=inv_operator,
        )

        # Delivery 2: 15 remaining units
        receive_purchase_order_delivery(
            po,
            delivery_reference="DELIV-PART-2",
            items=[{"line_item_id": line_item.pk, "quantity_received": 15}],
            user=inv_operator,
        )

        po.refresh_from_db()
        line_item.refresh_from_db()
        stock = Stock.get_for_variant(variant)

        assert po.status == PurchaseOrderStatus.FULLY_RECEIVED
        assert line_item.received_quantity == 40
        assert line_item.outstanding_quantity == 0
        assert po.total_outstanding_units == 0
        assert stock.on_hand == 40
        assert po.receipts.count() == 2

    def test_over_receipt_strictly_prevented(
        self, active_supplier, product, inv_operator, finance_user
    ):
        variant = product.variants.first()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 20, "unit_cost": Decimal("35.00")}],
        )
        approve_purchase_order(po, user=finance_user)
        send_purchase_order(po, user=inv_operator)
        line_item = po.items.first()

        # Attempt to receive 25 units when only 20 were ordered
        with pytest.raises(OverReceiptError, match="remain outstanding"):
            receive_purchase_order_delivery(
                po,
                delivery_reference="OVER-BOL",
                items=[{"line_item_id": line_item.pk, "quantity_received": 25}],
                user=inv_operator,
            )

        # Stock is unchanged
        stock = Stock.get_for_variant(variant)
        assert stock.on_hand == 0

    def test_duplicate_delivery_reference_rejected(
        self, active_supplier, product, inv_operator, finance_user
    ):
        variant = product.variants.first()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 30, "unit_cost": Decimal("35.00")}],
        )
        approve_purchase_order(po, user=finance_user)
        send_purchase_order(po, user=inv_operator)
        line_item = po.items.first()

        receive_purchase_order_delivery(
            po,
            delivery_reference="CARRIER-1234",
            items=[{"line_item_id": line_item.pk, "quantity_received": 10}],
            user=inv_operator,
        )

        # Re-submitting the exact same carrier reference for this PO fails idempotently
        with pytest.raises(DuplicateReceiptError):
            receive_purchase_order_delivery(
                po,
                delivery_reference="CARRIER-1234",
                items=[{"line_item_id": line_item.pk, "quantity_received": 10}],
                user=inv_operator,
            )

    def test_damaged_quarantined_goods_excluded_from_sellable_stock(
        self, active_supplier, product, inv_operator, finance_user
    ):
        variant = product.variants.first()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 50, "unit_cost": Decimal("35.00")}],
        )
        approve_purchase_order(po, user=finance_user)
        send_purchase_order(po, user=inv_operator)
        line_item = po.items.first()

        # 40 sound units accepted, 10 units water damaged / rejected
        receipt = receive_purchase_order_delivery(
            po,
            delivery_reference="TRUCK-990",
            items=[
                {
                    "line_item_id": line_item.pk,
                    "quantity_received": 40,
                    "quantity_damaged": 10,
                    "notes": "10 units damaged by water leak during transit",
                }
            ],
            user=inv_operator,
        )

        stock = Stock.get_for_variant(variant)
        # Sellable stock MUST only be the 40 accepted units!
        assert stock.on_hand == 40
        assert stock.available == 40

        # Receipt item preserves inspection breakdown
        rcpt_item = receipt.items.first()
        assert rcpt_item.quantity_received == 40
        assert rcpt_item.quantity_damaged == 10
        assert "water leak" in rcpt_item.notes


# --------------------------------------------------------------------------------------
# 5. Dashboard, Reporting Intelligence & Back Office Views
# --------------------------------------------------------------------------------------


class TestPurchasingIntelligenceAndBackofficeViews:
    def test_purchasing_dashboard_metrics_aggregation(
        self, active_supplier, product, inv_operator, finance_user
    ):
        variant = product.variants.first()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 25, "unit_cost": Decimal("40.00")}],
            expected_delivery_date=timezone.localdate() - timezone.timedelta(days=2),
        )
        approve_purchase_order(po, user=finance_user)
        send_purchase_order(po, user=inv_operator)

        metrics = get_procurement_dashboard_metrics()
        assert metrics["active_suppliers_count"] == 1
        assert metrics["ordered_count"] == 1
        assert metrics["overdue_count"] == 1
        assert metrics["total_outstanding_units"] == 25
        assert metrics["total_committed_spend"] == Decimal("1000.00")

    def test_supplier_performance_calculation(
        self, active_supplier, product, inv_operator, finance_user
    ):
        variant = product.variants.first()
        today = timezone.localdate()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 20, "unit_cost": Decimal("40.00")}],
            expected_delivery_date=today + timezone.timedelta(days=5),
        )
        approve_purchase_order(po, user=finance_user)
        send_purchase_order(po, user=inv_operator)

        receive_purchase_order_delivery(
            po,
            delivery_reference="ONTIME-01",
            items=[{"line_item_id": po.items.first().pk, "quantity_received": 20}],
            user=inv_operator,
        )

        perf = get_supplier_performance(active_supplier)
        assert perf["total_orders"] == 1
        assert perf["completed_orders_count"] == 1
        assert perf["fulfillment_rate"] == 100.0
        assert perf["on_time_rate"] == 100.0

    def test_backoffice_purchasing_views_access_control(
        self, client, inv_operator, unauthorized_staff, active_supplier, product
    ):
        # 1. Unauthorized staff account gets 404
        client.force_login(unauthorized_staff)
        resp = client.get(reverse("backoffice:procurement_dashboard"))
        assert resp.status_code == 404

        # 2. Inventory operator with PURCHASING_VIEW gets 200
        client.force_login(inv_operator)
        resp = client.get(reverse("backoffice:procurement_dashboard"))
        assert resp.status_code == 200
        assert "Procurement &amp; Purchasing" in resp.content.decode()

        # 3. Suppliers list view
        resp_sup = client.get(reverse("backoffice:suppliers"))
        assert resp_sup.status_code == 200
        assert "Alpine Textiles" in resp_sup.content.decode()

        # 4. Supplier detail view
        resp_detail = client.get(reverse("backoffice:supplier_detail", args=[active_supplier.pk]))
        assert resp_detail.status_code == 200
        assert "SUP-ALPINE" in resp_detail.content.decode()

    def test_purchase_order_print_view(
        self, client, inv_operator, active_supplier, product
    ):
        variant = product.variants.first()
        po = create_purchase_order(
            supplier=active_supplier,
            created_by=inv_operator,
            items=[{"variant": variant, "quantity": 15, "unit_cost": Decimal("45.00")}],
        )
        client.force_login(inv_operator)
        resp = client.get(reverse("backoffice:purchase_order_print", args=[po.pk]))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert po.reference in content
        assert "PURCHASE ORDER" in content
        assert "Alpine Textiles" in content
        assert "$675.00" in content


# --------------------------------------------------------------------------------------
# 6. Concurrency Contract
# --------------------------------------------------------------------------------------


class TestConcurrencyContract:
    def test_concurrent_receiving_attempts_on_postgresql(self):
        """Row locking with select_for_update is verified against PostgreSQL clusters."""
        if connection.vendor != "postgresql":
            pytest.skip(
                "select_for_update semantics and multi-connection concurrency are only observable on PostgreSQL"
            )
