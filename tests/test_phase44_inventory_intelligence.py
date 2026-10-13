"""Phase 44: Inventory Intelligence & Stock Operations test suite.

Covers:
- Available-to-sell calculations and counter invariants.
- Configurable low-stock threshold hierarchy (variant -> product -> global).
- Prioritized restock list with sales velocity and drop commitments.
- Restock receipts, ledger movements (kind=RECEIVED), and audit events.
- Reconciliation audit engine: detecting discrepancies (reserved counter mismatch,
  expired unswept holds, cancelled order holds, drop shortages, missing stock records).
- Transactional reconciliation actions (syncing reserved counters, sweeping holds).
- Operational alerts and cache-based deduplication.
- Back Office health dashboard, restock, reconciliation views, CSV export,
  and capability-based permissions (404 on missing capability).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.backoffice.models import AuditEvent
from apps.backoffice.permissions import (
    ADMIN_GROUP,
    FINANCE_GROUP,
    INVENTORY_GROUP,
    OPERATOR_GROUP,
    ORDERS_GROUP,
)
from apps.backoffice.services.alerts import alerts_for
from apps.catalog.models import Category, Product, ProductVariant
from apps.inventory.intelligence import (
    InventoryDiscrepancy,
    InventoryReconciliationReport,
    RestockItem,
    audit_inventory_reconciliation,
    dispatch_inventory_alert,
    get_inventory_health_summary,
    get_prioritized_restock_list,
    reconcile_cancelled_order_holds,
    reconcile_expired_holds,
    reconcile_variant_reserved,
    record_restock_receipt,
)
from apps.inventory.models import InventoryMovement, Reservation, Stock
from apps.inventory.services import (
    InsufficientStock,
    adjust_stock,
    consume_holds,
    release_holds,
    reserve_stock,
    sweep_expired_holds,
)
from apps.orders.models import Order
from tests.phase6_helpers import make_address, open_checkout, seed_stock

pytestmark = pytest.mark.django_db

PASSWORD = "Str0ng-Passw0rd!"


# --------------------------------------------------------------------------------------
# Helpers & Fixtures
# --------------------------------------------------------------------------------------


def staff_user(email: str, group_name: str | None = None):
    """Create an active staff user assigned to an optional back-office group."""
    user = get_user_model().objects.create_user(email=email, password=PASSWORD)
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    if group_name:
        group, _ = Group.objects.get_or_create(name=group_name)
        user.groups.add(group)
    return user


@pytest.fixture
def inv_operator():
    return staff_user("inv_op@flashwear.test", INVENTORY_GROUP)


@pytest.fixture
def finance_operator():
    return staff_user("finance_op@flashwear.test", FINANCE_GROUP)


@pytest.fixture
def general_operator():
    return staff_user("gen_op@flashwear.test", OPERATOR_GROUP)


# --------------------------------------------------------------------------------------
# 1. Available-to-Sell & Configurable Thresholds
# --------------------------------------------------------------------------------------


class TestThresholdHierarchyAndCounters:
    def test_available_to_sell_calculation(self, product):
        variant = product.variants.first()
        stock = seed_stock(variant, 15, reserved=4)

        assert stock.on_hand == 15
        assert stock.reserved == 4
        assert stock.available == 11
        assert variant.available_stock == 11

    def test_threshold_fallback_hierarchy(self, product):
        variant = product.variants.first()
        stock = Stock.get_for_variant(variant)

        # 1. Default fallback: setting (5)
        with override_settings(BACKOFFICE_LOW_STOCK_THRESHOLD=7):
            assert stock.effective_threshold == 7
            assert variant.effective_threshold == 7

        # 2. Product level threshold overrides global setting
        product.low_stock_threshold = 12
        product.save(update_fields=["low_stock_threshold"])
        stock.refresh_from_db()
        variant.refresh_from_db()

        assert stock.effective_threshold == 12
        assert variant.effective_threshold == 12

        # 3. Variant/Stock level threshold overrides product level
        stock.low_stock_threshold = 20
        stock.save(update_fields=["low_stock_threshold"])
        stock.refresh_from_db()
        variant.refresh_from_db()

        assert stock.effective_threshold == 20
        assert variant.effective_threshold == 20


# --------------------------------------------------------------------------------------
# 2. Prioritized Restock Management
# --------------------------------------------------------------------------------------


class TestPrioritizedRestockManagement:
    def test_restock_list_prioritizes_out_of_stock_and_velocity(self, product, make_product, colour, other_size):
        v1 = product.variants.first()
        stock1 = seed_stock(v1, 0, reserved=0)  # OOS -> CRITICAL

        prod2 = make_product(name="Oversized Hoodie", slug="oversized-hoodie")
        v2 = ProductVariant.objects.create(
            product=prod2,
            sku="HOOD-L-01",
            price=Decimal("89.00"),
            color=colour,
            size=other_size,
        )
        stock2 = seed_stock(v2, 3, reserved=0)  # Low stock (3 <= 5)
        stock2.low_stock_threshold = 5
        stock2.save(update_fields=["low_stock_threshold"])

        # Add recent sales movements for v2 to establish 30-day velocity
        now = timezone.now()
        for _ in range(4):
            InventoryMovement.objects.create(
                variant=v2,
                on_hand_delta=-2,
                reserved_delta=0,
                kind=InventoryMovement.Kind.SOLD,
                reference="order-test",
                created_at=now - timedelta(days=2),
            )

        items = get_prioritized_restock_list()
        assert len(items) >= 2

        # v1 is out of stock -> Priority CRITICAL
        item1 = next(it for it in items if it.variant_id == v1.pk)
        assert item1.priority == "CRITICAL"
        assert item1.on_hand == 0
        assert item1.suggested_restock_qty > 0

        # v2 is low stock with high sales velocity
        item2 = next(it for it in items if it.variant_id == v2.pk)
        assert item2.priority in ("CRITICAL", "URGENT", "WARNING")
        assert item2.recent_30d_sales == 8

    def test_restock_filtering(self, product, make_product, colour, other_size):
        v1 = product.variants.first()
        seed_stock(v1, 0, reserved=0)

        prod2 = make_product(name="Graphic Tee", slug="graphic-tee")
        v2 = ProductVariant.objects.create(
            product=prod2,
            sku="GRAPH-L-99",
            price=Decimal("45.00"),
            color=colour,
            size=other_size,
        )
        seed_stock(v2, 20, reserved=0)  # Well-stocked (20 > 5), not in low stock list

        # Query filter
        items_sku = get_prioritized_restock_list(query=v1.sku)
        assert len(items_sku) == 1
        assert items_sku[0].sku == v1.sku

        # Priority filter
        items_crit = get_prioritized_restock_list(priority_filter="CRITICAL")
        assert all(it.priority == "CRITICAL" for it in items_crit)

    def test_record_restock_receipt_success(self, product, inv_operator):
        variant = product.variants.first()
        stock = seed_stock(variant, 10, reserved=2)

        movement = record_restock_receipt(
            variant=variant,
            quantity=25,
            reason="PO-9842 Supplier Delivery",
            reference="CONTAINER-7",
            user=inv_operator,
            note="Inspected at dock A",
        )

        assert movement.kind == InventoryMovement.Kind.RECEIVED
        assert movement.on_hand_delta == 25
        assert movement.reserved_delta == 0
        assert movement.user == inv_operator
        assert movement.reference == "CONTAINER-7"

        stock.refresh_from_db()
        assert stock.on_hand == 35
        assert stock.available == 33

        # Verify audit trail
        audit = AuditEvent.objects.filter(action="inventory.restock", object_id=str(variant.pk)).first()
        assert audit is not None
        assert audit.actor == inv_operator
        assert audit.metadata["quantity"] == 25
        assert audit.metadata["reference"] == "CONTAINER-7"

    def test_record_restock_receipt_validations(self, product, inv_operator):
        variant = product.variants.first()

        # Zero quantity refused
        with pytest.raises(ValidationError, match="positive integer"):
            record_restock_receipt(variant=variant, quantity=0, reason="test", user=inv_operator)

        # Negative quantity refused
        with pytest.raises(ValidationError, match="positive integer"):
            record_restock_receipt(variant=variant, quantity=-5, reason="test", user=inv_operator)

        # Empty reason refused
        with pytest.raises(ValidationError, match="reason"):
            record_restock_receipt(variant=variant, quantity=10, reason="   ", user=inv_operator)


# --------------------------------------------------------------------------------------
# 3. Stock Accuracy & Reconciliation Engine
# --------------------------------------------------------------------------------------


class TestStockAccuracyAndReconciliation:
    def test_detects_reserved_counter_mismatch(self, product, user):
        variant = product.variants.first()
        stock = seed_stock(variant, 20, reserved=0)

        # Open checkout hold for 3 items -> creates Reservation(qty=3)
        checkout = open_checkout(user, variant=variant, quantity=3)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 3}], reference="test:hold")
        stock.refresh_from_db()
        assert stock.reserved == 3

        # Intentionally introduce counter corruption
        Stock.objects.filter(pk=stock.pk).update(reserved=1)

        report = audit_inventory_reconciliation()
        assert not report.is_consistent
        assert report.reserved_mismatches_count >= 1

        discrepancy = next(d for d in report.discrepancies if d.kind == "reserved_mismatch" and d.variant_id == variant.pk)
        assert discrepancy.recorded_reserved == 1
        assert discrepancy.calculated_reserved == 3
        assert discrepancy.severity.lower() in ("critical", "high")

    def test_reconcile_variant_reserved_fixes_mismatch(self, product, user, inv_operator):
        variant = product.variants.first()
        stock = seed_stock(variant, 20, reserved=0)
        checkout = open_checkout(user, variant=variant, quantity=4)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 4}], reference="test:hold")

        # Corrupt reserved count
        Stock.objects.filter(pk=stock.pk).update(reserved=1)

        result = reconcile_variant_reserved(variant=variant, actor=inv_operator, reason="Inventory audit sync")
        assert result["fixed"] is True
        assert result["old_reserved"] == 1
        assert result["new_reserved"] == 4

        stock.refresh_from_db()
        assert stock.reserved == 4
        assert stock.available == 16

        # Audit event recorded
        audit = AuditEvent.objects.filter(action="inventory.reconcile", object_id=str(variant.pk)).first()
        assert audit is not None
        assert audit.actor == inv_operator

    def test_detects_and_reconciles_expired_unswept_holds(self, product, user, inv_operator):
        variant = product.variants.first()
        stock = seed_stock(variant, 10, reserved=0)
        checkout = open_checkout(user, variant=variant, quantity=2)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 2}], reference="test:hold")

        # Force reservation to be expired in the past
        past = timezone.now() - timedelta(hours=2)
        Reservation.objects.filter(checkout=checkout).update(expires_at=past)

        report = audit_inventory_reconciliation()
        assert report.expired_holds_count >= 1

        # Reconcile expired holds
        reconciled = reconcile_expired_holds(actor=inv_operator)
        assert reconciled >= 1

        hold = Reservation.objects.filter(checkout=checkout).first()
        assert hold.status == Reservation.Status.EXPIRED

        stock.refresh_from_db()
        assert stock.reserved == 0
        assert stock.available == 10

    def test_detects_and_reconciles_cancelled_order_holds(self, product, user, address, inv_operator):
        variant = product.variants.first()
        stock = seed_stock(variant, 10, reserved=0)
        checkout = open_checkout(user, variant=variant, quantity=3)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 3}], reference="test:hold")

        # Create cancelled order linked to checkout
        order = Order.objects.create(
            number="FW-TEST-CANC-01",
            user=user,
            checkout=checkout,
            status=Order.Status.CANCELLED,
            currency="GBP",
            subtotal=Decimal("147.00"),
            shipping_amount=Decimal("0.00"),
            total=Decimal("147.00"),
        )
        Reservation.objects.filter(checkout=checkout).update(order=order)

        report = audit_inventory_reconciliation()
        assert report.cancelled_order_holds_count >= 1

        # Reconcile cancelled order holds
        released = reconcile_cancelled_order_holds(actor=inv_operator)
        assert released >= 1

        hold = Reservation.objects.filter(checkout=checkout).first()
        assert hold.status == Reservation.Status.RELEASED

        stock.refresh_from_db()
        assert stock.reserved == 0

    def test_detects_missing_stock_record(self, make_product, colour, size):
        prod = make_product(name="Brand New Tee", slug="brand-new-tee")
        v = ProductVariant.objects.create(
            product=prod,
            sku="BNT-M-01",
            price=Decimal("45.00"),
            color=colour,
            size=size,
        )
        # No Stock record created
        assert not Stock.objects.filter(variant=v).exists()

        report = audit_inventory_reconciliation()
        assert report.missing_stock_records_count >= 1
        d = next(disc for disc in report.discrepancies if disc.kind == "missing_stock_record" and disc.variant_id == v.pk)
        assert d.severity.lower() == "critical"


# --------------------------------------------------------------------------------------
# 4. Operational Alerts & Deduplication
# --------------------------------------------------------------------------------------


class TestOperationalAlertsAndDeduplication:
    def test_dispatch_inventory_alert_deduplication(self, product):
        variant = product.variants.first()
        cache.clear()

        # First alert sent
        sent1 = dispatch_inventory_alert(
            alert_type="out_of_stock",
            variant=variant,
            message="Variant is out of stock",
            dedup_window_seconds=300,
        )
        assert sent1 is True

        # Second alert in window is suppressed
        sent2 = dispatch_inventory_alert(
            alert_type="out_of_stock",
            variant=variant,
            message="Variant is out of stock",
            dedup_window_seconds=300,
        )
        assert sent2 is False

        # Different alert type on same variant is permitted
        sent3 = dispatch_inventory_alert(
            alert_type="low_stock",
            variant=variant,
            message="Variant is low stock",
            dedup_window_seconds=300,
        )
        assert sent3 is True

    def test_reconciliation_alert_integration_in_backoffice(self, product, user, inv_operator):
        variant = product.variants.first()
        stock = seed_stock(variant, 10, reserved=0)
        checkout = open_checkout(user, variant=variant, quantity=2)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 2}], reference="test:hold")

        # Corrupt reserved count
        Stock.objects.filter(pk=stock.pk).update(reserved=0)

        # Back office alerts should detect discrepancy
        active_alerts = alerts_for(inv_operator)

        rec_alert = next((a for a in active_alerts if a.key == "reconciliation_discrepancy"), None)
        assert rec_alert is not None
        assert rec_alert.severity in ("warning", "critical")
        assert "reconciliation" in rec_alert.title.lower()
        assert "backoffice:inventory_reconciliation" == rec_alert.url_name


# --------------------------------------------------------------------------------------
# 5. Back Office Views & Capabilities (Health, Restock, Reconciliation, CSV)
# --------------------------------------------------------------------------------------


class TestBackofficeInventoryIntelligenceViews:
    def test_health_view_accessible_to_inventory_operator(self, client, inv_operator, product):
        variant = product.variants.first()
        seed_stock(variant, 10, reserved=2)

        client.force_login(inv_operator)
        url = reverse("backoffice:inventory_health")
        response = client.get(url)

        assert response.status_code == 200
        assert "Inventory Health & Operations" in response.content.decode()
        assert "In Stock" in response.content.decode()

    def test_health_view_404_for_unauthorized_staff(self, client, finance_operator):
        # Finance does not carry INVENTORY_VIEW capability
        client.force_login(finance_operator)
        url = reverse("backoffice:inventory_health")
        response = client.get(url)

        assert response.status_code == 404

    def test_health_csv_export(self, client, inv_operator, product):
        variant = product.variants.first()
        seed_stock(variant, 10, reserved=2)

        client.force_login(inv_operator)
        url = reverse("backoffice:inventory_health_export")
        response = client.get(url)

        assert response.status_code == 200
        assert "text/csv" in response["Content-Type"]
        assert "attachment; filename=" in response["Content-Disposition"]
        content = response.content.decode("utf-8")
        assert "Variant ID,SKU,Product Name,Category" in content
        assert variant.sku in content

    def test_restock_view_and_receipt_submission(self, client, inv_operator, product):
        variant = product.variants.first()
        seed_stock(variant, 2, reserved=0)

        client.force_login(inv_operator)
        url = reverse("backoffice:inventory_restock")

        # GET restock prioritization screen
        response = client.get(url)
        assert response.status_code == 200
        assert "Restock Prioritization & Receipts" in response.content.decode()
        assert variant.sku in response.content.decode()

        # POST restock receipt
        post_data = {
            "variant_id": variant.pk,
            "quantity": 18,
            "reason": "Replenishment delivery batch B-44",
            "reference": "BOL-9901",
            "note": "Unpacked and stored in rack 3",
        }
        post_response = client.post(url, data=post_data, follow=True)
        assert post_response.status_code == 200

        stock = Stock.objects.get(variant=variant)
        assert stock.on_hand == 20
        assert stock.available == 20

    def test_reconciliation_view_and_actions(self, client, inv_operator, product, user):
        variant = product.variants.first()
        stock = seed_stock(variant, 15, reserved=0)
        checkout = open_checkout(user, variant=variant, quantity=3)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 3}], reference="test:hold")

        # Corrupt stock reserved count
        Stock.objects.filter(pk=stock.pk).update(reserved=1)

        client.force_login(inv_operator)
        url = reverse("backoffice:inventory_reconciliation")

        # GET reconciliation view
        response = client.get(url)
        assert response.status_code == 200
        assert "Stock Reconciliation" in response.content.decode()
        assert variant.sku in response.content.decode()

        # POST action: sync single variant
        action_url = reverse("backoffice:inventory_reconcile_action")
        act_resp = client.post(
            action_url,
            data={
                "action": "reconcile_variant",
                "variant_id": variant.pk,
                "reason": "Fix ledger drift from audit report",
            },
            follow=True,
        )
        assert act_resp.status_code == 200

        stock.refresh_from_db()
        assert stock.reserved == 3
        assert stock.available == 12

    def test_reconciliation_action_requires_inventory_manage(self, client, general_operator, product):
        # General operator has INVENTORY_VIEW but not INVENTORY_MANAGE
        client.force_login(general_operator)
        action_url = reverse("backoffice:inventory_reconcile_action")
        response = client.post(action_url, data={"action": "sweep_expired"})
        assert response.status_code == 404


# --------------------------------------------------------------------------------------
# 6. Drop Shortages, Inactive Stock & Historical Integrity
# --------------------------------------------------------------------------------------


class TestDropAndStorefrontConsistency:
    def test_drop_allocation_shortage_detected(self, product):
        from apps.drops.models import DropProduct, DropStatus, FlashDrop

        variant = product.variants.first()
        seed_stock(variant, 0, reserved=0)  # 0 available

        # Create scheduled drop with this variant
        now = timezone.now()
        drop = FlashDrop.objects.create(
            name="Neon Horizon Capsule",
            slug="neon-horizon-capsule",
            description="Limited release",
            starts_at=now + timedelta(days=2),
            ends_at=now + timedelta(days=4),
            status=DropStatus.SCHEDULED,
        )
        DropProduct.objects.create(
            drop=drop,
            product=product,
            variant=variant,
            order_position=1,
        )

        report = audit_inventory_reconciliation()
        shortage = next((d for d in report.discrepancies if d.kind == "drop_allocation_shortage"), None)
        assert shortage is not None
        assert shortage.variant_id == variant.pk

    def test_inactive_variant_holding_stock_detected(self, product):
        variant = product.variants.first()
        seed_stock(variant, 15, reserved=0)

        # Deactivate variant
        variant.is_active = False
        variant.save(update_fields=["is_active"])

        report = audit_inventory_reconciliation()
        inactive_disc = next((d for d in report.discrepancies if d.kind in ("stock_on_inactive_variant", "inactive_variant_stock")), None)
        assert inactive_disc is not None
        assert inactive_disc.variant_id == variant.pk

    def test_reconciliation_preserves_historical_orders_and_ledger(self, product, user, address, inv_operator):
        from apps.orders.models import OrderAddress, OrderItem

        variant = product.variants.first()
        seed_stock(variant, 20, reserved=0)
        checkout = open_checkout(user, variant=variant, quantity=2)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 2}], reference="test:hold")

        # Snapshot order
        order = Order.objects.create(
            number="FW-HIST-SNAP-01",
            user=user,
            checkout=checkout,
            status=Order.Status.PAID,
            currency="GBP",
            subtotal=Decimal("98.00"),
            shipping_amount=Decimal("0.00"),
            total=Decimal("98.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            variant=variant,
            sku=variant.sku,
            product_name="Original Heavyweight Tee",
            unit_price=Decimal("49.00"),
            quantity=2,
            line_total=Decimal("98.00"),
        )
        addr = OrderAddress.objects.create(
            order=order,
            full_name=address.full_name,
            line1=address.line1,
            city=address.city,
            postal_code=address.postal_code,
            country=address.country,
        )

        # Trigger reconciliation
        reconcile_variant_reserved(variant=variant, actor=inv_operator, reason="Nightly reconciliation check")

        # Assert frozen order snapshots are untouched
        item.refresh_from_db()
        addr.refresh_from_db()
        order.refresh_from_db()

        assert item.product_name == "Original Heavyweight Tee"
        assert item.unit_price == Decimal("49.00")
        assert item.quantity == 2
        assert addr.full_name == "Ada Lovelace"
        assert order.status == Order.Status.PAID


# --------------------------------------------------------------------------------------
# 7. Concurrency Contract (PostgreSQL Only)
# --------------------------------------------------------------------------------------


class TestConcurrencyContract:
    @pytest.mark.django_db(transaction=True)
    def test_concurrent_stock_receipts_on_postgresql(self, db, inv_operator, product):
        if connection.vendor != "postgresql":
            pytest.skip("select_for_update semantics and multi-connection concurrency are only observable on PostgreSQL")

        import threading
        from django.db import connections

        variant = product.variants.first()
        seed_stock(variant, 10)

        results: list[int] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2, timeout=15)

        def receive(qty):
            try:
                barrier.wait()
                mov = record_restock_receipt(
                    variant=variant,
                    quantity=qty,
                    reason="Concurrent shipment receipt",
                    reference="BOL-CONCURRENT",
                    actor=inv_operator,
                )
                results.append(mov.on_hand_delta)
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        t1 = threading.Thread(target=receive, args=(5,))
        t2 = threading.Thread(target=receive, args=(10,))

        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not errors, errors
        assert sorted(results) == [5, 10]
        stock = Stock.objects.get(variant=variant)
        assert stock.on_hand == 25
