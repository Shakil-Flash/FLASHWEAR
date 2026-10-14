"""Automated tests for FLASHWEAR Phase 62: Inventory & Stock Operations.

Covers:
1. Inventory workspace access & RBAC boundaries across all staff roles.
2. Search, pagination, and multi-attribute filters (low stock, out of stock, category, brand).
3. Variant stock detail inspection (stock counters, active holds, ledger movements).
4. Safe stock adjustments (discrepancy corrections, damage write-off, loss/shrinkage).
5. Stock receiving workflow.
6. Validation guards (positive quantities, mandatory reasons, insufficient stock prevention).
7. Duplicate submission / idempotency guard protection.
8. Compensating reversals by authorized managers and ledger immutability.
9. Low-stock reorder thresholds (variant override vs inherited product/global default).
10. Reservation consistency (holds, checkout, fulfillment, cancellation).
11. Audit logging and Staff Dashboard indicators integration.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.backoffice.models import AuditEvent
from apps.catalog.models import Brand, Category, Color, Product, ProductVariant, Size
from apps.inventory.models import InventoryMovement, Stock
from apps.inventory.services import (
    consume_holds,
    release_holds,
    reserve_stock,
)
from apps.staff.constants import ROLE_PRIMARY_GROUP, StaffRole
from apps.staff.dashboard_services import (
    get_daily_operations_summary,
    get_my_work_and_team_queues,
    get_quick_actions,
)
from apps.staff.inventory_services import (
    record_compensating_reversal,
    record_stock_adjustment,
)
from tests.phase6_helpers import open_checkout

User = get_user_model()


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def owner_user(db):
    u = User.objects.create_superuser(
        email="owner.inv@flashwear.com",
        password="ValidPassword123!",
        first_name="Owner",
        last_name="Boss",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.OWNER])
    u.groups.add(g)
    return u


@pytest.fixture
def ops_manager(db):
    u = User.objects.create_user(
        email="ops.inv@flashwear.com",
        password="ValidPassword123!",
        first_name="Ops",
        last_name="Manager",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.OPERATIONS_MANAGER])
    u.groups.add(g)
    return u


@pytest.fixture
def inventory_staff(db):
    u = User.objects.create_user(
        email="inv.staff@flashwear.com",
        password="ValidPassword123!",
        first_name="Stock",
        last_name="Keeper",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.INVENTORY_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def product_manager(db):
    u = User.objects.create_user(
        email="prod.mgr@flashwear.com",
        password="ValidPassword123!",
        first_name="Product",
        last_name="Manager",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.PRODUCT_MANAGER])
    u.groups.add(g)
    return u


@pytest.fixture
def order_staff(db):
    u = User.objects.create_user(
        email="order.inv@flashwear.com",
        password="ValidPassword123!",
        first_name="Order",
        last_name="Staff",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.ORDER_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def delivery_staff(db):
    u = User.objects.create_user(
        email="driver.inv@flashwear.com",
        password="ValidPassword123!",
        first_name="Delivery",
        last_name="Driver",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.DELIVERY_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def readonly_staff(db):
    u = User.objects.create_user(
        email="audit.inv@flashwear.com",
        password="ValidPassword123!",
        first_name="Auditor",
        last_name="Viewer",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.READ_ONLY])
    u.groups.add(g)
    return u


@pytest.fixture
def shopper_user(db):
    return User.objects.create_user(
        email="shopper.phase62@flashwear.com",
        password="ValidPassword123!",
    )


@pytest.fixture
def sample_catalog(db):
    cat = Category.objects.create(name="T-Shirts", slug="t-shirts")
    brand = Brand.objects.create(name="FlashAero", slug="flashaero")
    color = Color.objects.create(name="Jet Black", hex_code="#000000")
    size_m = Size.objects.create(name="Medium", code="M", display_order=2)
    size_l = Size.objects.create(name="Large", code="L", display_order=3)

    product = Product.objects.create(
        name="Tech Cotton Tee",
        slug="tech-cotton-tee",
        category=cat,
        brand=brand,
        status=Product.Status.ACTIVE,
        low_stock_threshold=10,
    )

    v1 = ProductVariant.objects.create(
        product=product,
        sku="TEE-BLK-M",
        barcode="880011223344",
        color=color,
        size=size_m,
        price=Decimal("49.99"),
        is_active=True,
    )
    # Give initial stock of 20
    s1 = Stock.get_for_variant(v1)
    s1.on_hand = 20
    s1.save()

    v2 = ProductVariant.objects.create(
        product=product,
        sku="TEE-BLK-L",
        barcode="880011223355",
        color=color,
        size=size_l,
        price=Decimal("49.99"),
        is_active=True,
    )
    # Low stock variant: on_hand = 4 (threshold is 10)
    s2 = Stock.get_for_variant(v2)
    s2.on_hand = 4
    s2.low_stock_threshold = 8
    s2.save()

    # Out of stock variant
    size_s = Size.objects.create(name="Small", code="S", display_order=1)
    v3 = ProductVariant.objects.create(
        product=product,
        sku="TEE-BLK-S",
        color=color,
        size=size_s,
        price=Decimal("49.99"),
        is_active=True,
    )
    s3 = Stock.get_for_variant(v3)
    s3.on_hand = 0
    s3.save()

    return {
        "category": cat,
        "brand": brand,
        "product": product,
        "v1": v1,
        "s1": s1,
        "v2": v2,
        "s2": s2,
        "v3": v3,
        "s3": s3,
    }


# =============================================================================
# 1. Role-Based Access Control Tests
# =============================================================================


class TestInventoryRoleAccess:
    """Verify visibility and mutation boundaries across all staff roles."""

    def test_anonymous_redirected_to_login(self, client):
        url = reverse("staff:inventory")
        resp = client.get(url)
        assert resp.status_code == 302
        assert "/staff/login/" in resp.url

    def test_delivery_staff_denied_inventory_workspace(self, client, delivery_staff):
        client.force_login(delivery_staff)
        url = reverse("staff:inventory")
        resp = client.get(url)
        assert resp.status_code == 403

    def test_inventory_staff_can_view_workspace(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        url = reverse("staff:inventory")
        resp = client.get(url)
        assert resp.status_code == 200
        assert "Inventory & Stock Operations" in resp.content.decode()
        assert "TEE-BLK-M" in resp.content.decode()

    def test_product_manager_can_view_but_cannot_adjust(
        self, client, product_manager, sample_catalog
    ):
        client.force_login(product_manager)
        v = sample_catalog["v1"]

        # Product manager can view
        resp = client.get(reverse("staff:inventory"))
        assert resp.status_code == 200
        assert resp.context["can_manage"] is False

        # Attempting adjustment mutation is blocked
        adjust_url = reverse("staff:inventory_adjust_action", kwargs={"variant_id": v.pk})
        post_data = {
            "adjustment_type": "correction_add",
            "quantity": 5,
            "reason": "Unauthorized attempt",
        }
        resp = client.post(adjust_url, post_data)
        assert resp.status_code == 403

    def test_readonly_staff_can_view_detail_without_forms(
        self, client, readonly_staff, sample_catalog
    ):
        client.force_login(readonly_staff)
        v = sample_catalog["v1"]
        resp = client.get(reverse("staff:inventory_variant_detail", kwargs={"variant_id": v.pk}))
        assert resp.status_code == 200
        assert "Read-Only Inventory Access" in resp.content.decode()

    def test_order_staff_can_view_inventory(self, client, order_staff, sample_catalog):
        client.force_login(order_staff)
        resp = client.get(reverse("staff:inventory"))
        assert resp.status_code == 200


# =============================================================================
# 2. Workspace Search and Filtering Tests
# =============================================================================


class TestInventorySearchAndFilter:
    """Test multi-attribute filtering, search, and KPI calculations."""

    def test_filter_low_stock(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        resp = client.get(f"{reverse('staff:inventory')}?status=low_stock")
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "TEE-BLK-L" in content
        assert "TEE-BLK-M" not in content

    def test_filter_out_of_stock(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        resp = client.get(f"{reverse('staff:inventory')}?status=out_of_stock")
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "TEE-BLK-S" in content
        assert "TEE-BLK-M" not in content

    def test_search_by_sku_and_barcode(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        # Search SKU
        resp = client.get(f"{reverse('staff:inventory')}?q=TEE-BLK-M")
        assert resp.status_code == 200
        assert "TEE-BLK-M" in resp.content.decode()
        assert "TEE-BLK-L" not in resp.content.decode()

        # Search Barcode
        resp = client.get(f"{reverse('staff:inventory')}?q=880011223355")
        assert resp.status_code == 200
        assert "TEE-BLK-L" in resp.content.decode()

    def test_filter_by_category_and_brand(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        resp = client.get(f"{reverse('staff:inventory')}?category=t-shirts&brand=flashaero")
        assert resp.status_code == 200
        assert resp.context["kpis"]["total_skus"] == 3


# =============================================================================
# 3. Safe Stock Adjustments Tests
# =============================================================================


class TestStockAdjustments:
    """Test additions, deductions, damaged items, loss write-offs, and validations."""

    def test_receive_new_stock_success(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        v = sample_catalog["v1"]
        init_on_hand = sample_catalog["s1"].on_hand

        receive_url = reverse("staff:inventory_receive_action", kwargs={"variant_id": v.pk})
        resp = client.post(
            receive_url,
            {
                "quantity": 15,
                "supplier_reference": "PO-2026-001",
                "reason": "Batch arrival from supplier",
                "idempotency_token": "token-receive-123",
            },
        )
        assert resp.status_code == 302

        stock = Stock.get_for_variant(v)
        assert stock.on_hand == init_on_hand + 15
        assert stock.available == init_on_hand + 15

        # Verify Movement record
        movement = InventoryMovement.objects.filter(
            variant=v, reference="token-receive-123"
        ).first()
        assert movement is not None
        assert movement.kind == InventoryMovement.Kind.RECEIVED
        assert movement.on_hand_delta == 15
        assert movement.user == inventory_staff

        # Verify AuditEvent
        audit = AuditEvent.objects.filter(action="inventory.received", object_id=str(v.pk)).first()
        assert audit is not None
        assert audit.actor == inventory_staff

    def test_discrepancy_addition_adjustment(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        v = sample_catalog["v1"]
        stock_before = Stock.get_for_variant(v).on_hand

        adjust_url = reverse("staff:inventory_adjust_action", kwargs={"variant_id": v.pk})
        resp = client.post(
            adjust_url,
            {
                "adjustment_type": "correction_add",
                "quantity": 3,
                "reason": "Found extra unit behind pallet 4",
                "reference": "CYCLE-COUNT-9",
            },
        )
        assert resp.status_code == 302
        stock = Stock.get_for_variant(v)
        assert stock.on_hand == stock_before + 3

    def test_damaged_stock_deduction(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        v = sample_catalog["v1"]
        stock_before = Stock.get_for_variant(v).on_hand

        adjust_url = reverse("staff:inventory_adjust_action", kwargs={"variant_id": v.pk})
        resp = client.post(
            adjust_url,
            {
                "adjustment_type": "damaged",
                "quantity": 2,
                "reason": "Water damage from roof leak in section B",
            },
        )
        assert resp.status_code == 302
        stock = Stock.get_for_variant(v)
        assert stock.on_hand == stock_before - 2

        # Verify movement kind
        m = InventoryMovement.objects.filter(variant=v, kind=InventoryMovement.Kind.DAMAGED).first()
        assert m is not None
        assert m.on_hand_delta == -2

    def test_stock_loss_shrinkage_deduction(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        v = sample_catalog["v1"]
        stock_before = Stock.get_for_variant(v).on_hand

        adjust_url = reverse("staff:inventory_adjust_action", kwargs={"variant_id": v.pk})
        resp = client.post(
            adjust_url,
            {
                "adjustment_type": "loss",
                "quantity": 1,
                "reason": "Unaccounted shortage post quarterly count",
            },
        )
        assert resp.status_code == 302
        stock = Stock.get_for_variant(v)
        assert stock.on_hand == stock_before - 1

    def test_insufficient_stock_deduction_fails(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        v = sample_catalog["v2"]  # has 4 units on hand

        adjust_url = reverse("staff:inventory_adjust_action", kwargs={"variant_id": v.pk})
        resp = client.post(
            adjust_url,
            {
                "adjustment_type": "correction_sub",
                "quantity": 10,  # attempts to deduct 10 from 4
                "reason": "Over-deduction attempt",
            },
        )
        assert resp.status_code == 302
        stock = Stock.get_for_variant(v)
        assert stock.on_hand == 4  # Unchanged

    def test_adjustment_missing_reason_rejected(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        v = sample_catalog["v1"]

        adjust_url = reverse("staff:inventory_adjust_action", kwargs={"variant_id": v.pk})
        resp = client.post(
            adjust_url,
            {
                "adjustment_type": "correction_add",
                "quantity": 5,
                "reason": "",  # Empty reason
            },
        )
        assert resp.status_code == 302
        stock = Stock.get_for_variant(v)
        assert stock.on_hand == 20  # Unchanged

    def test_adjustment_negative_or_zero_quantity_rejected(
        self, client, inventory_staff, sample_catalog
    ):
        client.force_login(inventory_staff)
        v = sample_catalog["v1"]

        adjust_url = reverse("staff:inventory_adjust_action", kwargs={"variant_id": v.pk})
        resp = client.post(
            adjust_url,
            {
                "adjustment_type": "correction_add",
                "quantity": -5,
                "reason": "Negative quantity",
            },
        )
        assert resp.status_code == 302
        stock = Stock.get_for_variant(v)
        assert stock.on_hand == 20


# =============================================================================
# 4. Idempotency Guard Tests
# =============================================================================


class TestInventoryIdempotency:
    """Verify duplicate submission prevention."""

    def test_duplicate_submission_does_not_double_adjust(self, inventory_staff, sample_catalog):
        v = sample_catalog["v1"]
        token = f"idem-{uuid.uuid4().hex}"

        # First adjustment
        stock1, m1 = record_stock_adjustment(
            v.pk,
            adjustment_type="received",
            quantity=10,
            reason="Pallet drop",
            actor=inventory_staff,
            idempotency_token=token,
        )
        assert stock1.on_hand == 30

        # Duplicate adjustment with same token within 5 minutes
        stock2, m2 = record_stock_adjustment(
            v.pk,
            adjustment_type="received",
            quantity=10,
            reason="Pallet drop duplicate retry",
            actor=inventory_staff,
            idempotency_token=token,
        )
        # Should return existing movement without adjusting again
        assert stock2.on_hand == 30
        assert m1.pk == m2.pk


# =============================================================================
# 5. Compensating Reversals & Ledger Immutability Tests
# =============================================================================


class TestStockLedgerAndReversals:
    """Verify append-only ledger and compensating reversals."""

    def test_reversal_by_manager_succeeds(
        self, client, ops_manager, inventory_staff, sample_catalog
    ):
        v = sample_catalog["v1"]

        # Step 1: Staff records an erroneous receipt of 10 units
        stock, original_m = record_stock_adjustment(
            v.pk,
            adjustment_type="received",
            quantity=10,
            reason="Erroneous receipt",
            actor=inventory_staff,
        )
        assert stock.on_hand == 30

        # Step 2: Manager executes compensating reversal
        client.force_login(ops_manager)
        rev_url = reverse("staff:inventory_reverse_action", kwargs={"movement_id": original_m.pk})
        resp = client.post(
            rev_url, {"movement_id": original_m.pk, "reason": "Supplier confirmed wrong pallet"}
        )
        assert resp.status_code == 302

        # Stock should be compensated back to 20
        stock.refresh_from_db()
        assert stock.on_hand == 20

        # Verify compensating reversal movement exists
        rev_m = InventoryMovement.objects.filter(reference=f"reversal:{original_m.pk}").first()
        assert rev_m is not None
        assert rev_m.kind == InventoryMovement.Kind.REVERSAL
        assert rev_m.on_hand_delta == -10
        assert rev_m.user == ops_manager

        # Original movement is preserved!
        original_m.refresh_from_db()
        assert original_m.on_hand_delta == 10

    def test_regular_staff_cannot_authorize_reversal(self, client, inventory_staff, sample_catalog):
        v = sample_catalog["v1"]
        _, original_m = record_stock_adjustment(
            v.pk,
            adjustment_type="received",
            quantity=5,
            reason="Normal receipt",
            actor=inventory_staff,
        )

        client.force_login(inventory_staff)
        rev_url = reverse("staff:inventory_reverse_action", kwargs={"movement_id": original_m.pk})
        resp = client.post(rev_url, {"movement_id": original_m.pk, "reason": "Self-reversal"})
        assert resp.status_code == 403

    def test_cannot_reverse_same_movement_twice(self, ops_manager, inventory_staff, sample_catalog):
        v = sample_catalog["v1"]
        _, original_m = record_stock_adjustment(
            v.pk,
            adjustment_type="received",
            quantity=5,
            reason="To reverse",
            actor=inventory_staff,
        )

        # First reversal
        record_compensating_reversal(
            original_m.pk,
            reason="Mistake",
            actor=ops_manager,
        )

        # Second reversal attempt raises ValidationError
        with pytest.raises(ValidationError, match="already been reversed"):
            record_compensating_reversal(
                original_m.pk,
                reason="Attempt duplicate reversal",
                actor=ops_manager,
            )

    def test_cannot_reverse_non_manual_movement(self, ops_manager, shopper_user, sample_catalog):
        v = sample_catalog["v1"]
        # System movement kind like SOLD or RESERVED cannot be manually reversed
        checkout = open_checkout(shopper_user, variant=v)
        holds = reserve_stock(checkout, [{"variant_id": v.pk, "quantity": 2}])
        consume_holds(holds)

        consumed_m = InventoryMovement.objects.filter(
            variant=v, kind=InventoryMovement.Kind.SOLD
        ).first()
        assert consumed_m is not None

        with pytest.raises(ValidationError, match="Cannot reverse movement kind"):
            record_compensating_reversal(
                consumed_m.pk,
                reason="Attempt to reverse order fulfillment",
                actor=ops_manager,
            )


# =============================================================================
# 6. Reorder Threshold and Replenishment Tests
# =============================================================================


class TestStockThresholdAndReplenishment:
    """Test reorder threshold updates and replenishment calculation."""

    def test_update_variant_threshold(self, client, inventory_staff, sample_catalog):
        client.force_login(inventory_staff)
        v = sample_catalog["v1"]

        thresh_url = reverse("staff:inventory_threshold_action", kwargs={"variant_id": v.pk})
        resp = client.post(thresh_url, {"low_stock_threshold": 12})
        assert resp.status_code == 302

        stock = Stock.get_for_variant(v)
        assert stock.low_stock_threshold == 12

    def test_clear_variant_threshold_inherits_product_default(
        self, client, inventory_staff, sample_catalog
    ):
        client.force_login(inventory_staff)
        v = sample_catalog["v2"]  # had low_stock_threshold = 8

        thresh_url = reverse("staff:inventory_threshold_action", kwargs={"variant_id": v.pk})
        resp = client.post(thresh_url, {"low_stock_threshold": ""})
        assert resp.status_code == 302

        stock = Stock.get_for_variant(v)
        assert stock.low_stock_threshold is None
        # Inherits product threshold of 10
        assert stock.effective_threshold == 10

    def test_replenishment_suggestion_calculation(self, sample_catalog):
        v2 = sample_catalog["v2"]
        stock = Stock.get_for_variant(v2)
        # on_hand is 4, low_stock_threshold is 8
        # Suggested replenishment is max(8*2, 8+10) - on_hand = 18 - 4 = 14
        assert stock.suggested_replenishment == 14


# =============================================================================
# 7. Reservation Consistency Tests
# =============================================================================


class TestStockReservationConsistency:
    """Verify reservations do not affect on_hand, but correctly deduct from available."""

    def test_reservation_hold_deducts_available_not_on_hand(self, shopper_user, sample_catalog):
        v = sample_catalog["v1"]
        stock = Stock.get_for_variant(v)
        assert stock.on_hand == 20
        assert stock.available == 20

        checkout = open_checkout(shopper_user, variant=v)
        holds = reserve_stock(
            checkout, [{"variant_id": v.pk, "quantity": 5}], reference="test:hold"
        )
        stock.refresh_from_db()
        assert stock.on_hand == 20
        assert stock.reserved == 5
        assert stock.available == 15

        # Release hold
        release_holds(holds)
        stock.refresh_from_db()
        assert stock.on_hand == 20
        assert stock.reserved == 0
        assert stock.available == 20

    def test_fulfillment_consumes_both_on_hand_and_reserved(self, shopper_user, sample_catalog):
        v = sample_catalog["v1"]
        stock = Stock.get_for_variant(v)

        checkout = open_checkout(shopper_user, variant=v)
        holds = reserve_stock(
            checkout, [{"variant_id": v.pk, "quantity": 4}], reference="test:consume"
        )
        consume_holds(holds)

        stock.refresh_from_db()
        assert stock.on_hand == 16
        assert stock.reserved == 0
        assert stock.available == 16


# =============================================================================
# 8. Dashboard Integration Tests
# =============================================================================


class TestStaffDashboardInventoryIntegration:
    """Verify inventory indicators and alerts on the staff dashboard."""

    def test_dashboard_summary_includes_inventory_metrics(self, inventory_staff, sample_catalog):
        summary = get_daily_operations_summary(inventory_staff)
        assert "inventory" in summary
        inv_data = summary["inventory"]
        assert inv_data["total_tracked"] == 3
        assert inv_data["out_of_stock"] == 1
        assert inv_data["low_stock"] == 1

    def test_dashboard_urgent_alerts_contain_stockouts(self, inventory_staff, sample_catalog):
        queues = get_my_work_and_team_queues(inventory_staff)
        urgent = queues["urgent_alerts"]
        titles = [a["title"] for a in urgent]
        assert any("Stockout Risk: TEE-BLK-S" in t for t in titles)

    def test_quick_actions_include_inventory_links(self, inventory_staff):
        actions = get_quick_actions(inventory_staff)
        keys = [a["key"] for a in actions]
        assert "inventory_low_stock" in keys
        assert "inventory_ledger" in keys
