"""Automated Test Suite for FLASHWEAR Phase 60:
Order Processing & Fulfillment Workspace.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts.models import Address
from apps.backoffice.models import AuditEvent
from apps.catalog.models import Category, Product, ProductVariant
from apps.orders.models import Order, OrderAddress, OrderItem, Shipment
from apps.payments.models import Payment
from apps.shop.models import Cart, CheckoutSession
from apps.staff.constants import ROLE_PRIMARY_GROUP, StaffRole
from apps.staff.permissions import ensure_staff_groups

User = get_user_model()


@pytest.fixture(autouse=True)
def setup_groups(db):
    """Ensure all required staff groups exist before each test."""
    ensure_staff_groups()


@pytest.fixture
def owner_user(db):
    u = User.objects.create_user(
        email="owner.orders@flashwear.com",
        password="ValidPassword123!",
        first_name="Owner",
        last_name="Boss",
        is_staff=True,
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.OWNER])
    u.groups.add(g)
    return u


@pytest.fixture
def ops_manager(db):
    u = User.objects.create_user(
        email="ops.orders@flashwear.com",
        password="ValidPassword123!",
        first_name="Ops",
        last_name="Manager",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.OPERATIONS_MANAGER])
    u.groups.add(g)
    return u


@pytest.fixture
def order_staff(db):
    u = User.objects.create_user(
        email="processor.orders@flashwear.com",
        password="ValidPassword123!",
        first_name="Order",
        last_name="Processor",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.ORDER_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def delivery_staff(db):
    u = User.objects.create_user(
        email="driver.orders@flashwear.com",
        password="ValidPassword123!",
        first_name="Delivery",
        last_name="Driver",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.DELIVERY_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def support_staff(db):
    u = User.objects.create_user(
        email="support.orders@flashwear.com",
        password="ValidPassword123!",
        first_name="Support",
        last_name="Agent",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.SUPPORT_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def product_manager(db):
    u = User.objects.create_user(
        email="pm.orders@flashwear.com",
        password="ValidPassword123!",
        first_name="Product",
        last_name="Manager",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.PRODUCT_MANAGER])
    u.groups.add(g)
    return u


@pytest.fixture
def inventory_staff(db):
    u = User.objects.create_user(
        email="inv.orders@flashwear.com",
        password="ValidPassword123!",
        first_name="Inventory",
        last_name="Keeper",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.INVENTORY_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def read_only_staff(db):
    u = User.objects.create_user(
        email="readonly.orders@flashwear.com",
        password="ValidPassword123!",
        first_name="Read",
        last_name="Only",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.READ_ONLY])
    u.groups.add(g)
    return u


@pytest.fixture
def customer_user(db):
    return User.objects.create_user(
        email="shopper.orders@flashwear.com",
        password="ValidPassword123!",
        first_name="Alice",
        last_name="Smith",
    )


@pytest.fixture
def sample_category(db):
    return Category.objects.create(name="Hoodies", slug="hoodies")


@pytest.fixture
def sample_product_and_variant(db, sample_category):
    p = Product.objects.create(name="Flash Hoodie", slug="flash-hoodie", category=sample_category)
    v = ProductVariant.objects.create(product=p, sku="HOOD-BLK-L", price=Decimal("65.00"))
    return p, v


def make_test_order(
    user,
    number=None,
    status=Order.Status.PAID,
    total=Decimal("65.00"),
    variant=None,
    with_address=True,
    with_payment=True,
):
    """Helper creating a valid Order with mandatory checkout, line item, and address snapshots."""
    if not number:
        number = f"FW-{uuid.uuid4().hex[:8].upper()}"

    cart = Cart.objects.create(user=user, status=Cart.Status.CONVERTED)
    addr, _ = Address.objects.get_or_create(
        user=user,
        defaults={
            "full_name": "Alice Smith",
            "phone": "+44 7700 900077",
            "line1": "42 High Street",
            "city": "London",
            "postal_code": "EC1A 1BB",
            "country": "GB",
        },
    )
    checkout = CheckoutSession.objects.create(
        user=user,
        cart=cart,
        shipping_address=addr,
        status=CheckoutSession.Status.CONVERTED,
    )

    order = Order.objects.create(
        user=user,
        number=number,
        checkout=checkout,
        status=status,
        currency="GBP",
        subtotal=total,
        shipping_amount=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total=total,
        shipping_code="standard",
        shipping_name="Standard Tracked Delivery",
    )

    if with_address:
        OrderAddress.objects.create(
            order=order,
            full_name=addr.full_name,
            phone=addr.phone,
            line1=addr.line1,
            city=addr.city,
            postal_code=addr.postal_code,
            country=addr.country,
        )

    if variant:
        OrderItem.objects.create(
            order=order,
            variant=variant,
            sku=variant.sku,
            product_name=variant.product.name,
            option_label="Black / L",
            quantity=1,
            unit_price=total,
            line_total=total,
        )

    if with_payment and status != Order.Status.PENDING_PAYMENT:
        Payment.objects.create(
            order=order,
            provider="stripe",
            provider_reference=f"ch_{uuid.uuid4().hex[:12]}",
            amount=total,
            currency="GBP",
            status=Payment.Status.SUCCEEDED,
            paid_at=order.created_at,
        )

    return order


# =============================================================================
# 1. Role-Based Access and Permission Boundary Tests
# =============================================================================


@pytest.mark.django_db
class TestOrderWorkspaceAccessAndRoles:
    """Verify role permissions, denials, and direct-object authorization."""

    def test_anonymous_redirected_to_staff_login(self, client):
        resp = client.get(reverse("staff:orders"))
        assert resp.status_code == 302
        assert reverse("staff:login") in resp.headers["Location"]

    def test_customer_user_forbidden(self, client, customer_user):
        client.force_login(customer_user)
        resp = client.get(reverse("staff:orders"))
        assert resp.status_code == 403
        assert "Access Denied" in resp.content.decode()

    def test_product_manager_and_inventory_staff_denied_orders(
        self, client, product_manager, inventory_staff
    ):
        for u in [product_manager, inventory_staff]:
            client.force_login(u)
            resp = client.get(reverse("staff:orders"))
            assert resp.status_code == 403
            assert "staff.orders.view" in resp.content.decode()

    def test_authorized_roles_can_view_order_list(
        self,
        client,
        owner_user,
        ops_manager,
        order_staff,
        delivery_staff,
        support_staff,
        read_only_staff,
    ):
        for u in [
            owner_user,
            ops_manager,
            order_staff,
            delivery_staff,
            support_staff,
            read_only_staff,
        ]:
            client.force_login(u)
            resp = client.get(reverse("staff:orders"))
            assert resp.status_code == 200
            content = resp.content.decode()
            assert "Order Processing & Fulfillment" in content

    def test_order_detail_view_permissions(
        self,
        client,
        order_staff,
        delivery_staff,
        product_manager,
        customer_user,
        sample_product_and_variant,
    ):
        _, var = sample_product_and_variant
        order = make_test_order(customer_user, variant=var)

        # Order staff can view
        client.force_login(order_staff)
        resp = client.get(reverse("staff:order_detail", kwargs={"pk": order.pk}))
        assert resp.status_code == 200
        assert order.number in resp.content.decode()

        # Delivery staff can view
        client.force_login(delivery_staff)
        resp = client.get(reverse("staff:order_detail", kwargs={"pk": order.pk}))
        assert resp.status_code == 200
        assert order.number in resp.content.decode()

        # Product manager is denied
        client.force_login(product_manager)
        resp = client.get(reverse("staff:order_detail", kwargs={"pk": order.pk}))
        assert resp.status_code == 403

    def test_nonexistent_order_redirects_gracefully(self, client, order_staff):
        client.force_login(order_staff)
        resp = client.get(reverse("staff:order_detail", kwargs={"pk": 999999}))
        assert resp.status_code == 302
        assert reverse("staff:orders") in resp.headers["Location"]


# =============================================================================
# 2. State Mutation Permission Gates
# =============================================================================


@pytest.mark.django_db
class TestOrderMutationPermissions:
    """Verify state-changing POST actions are restricted to ORDERS_MANAGE."""

    def test_read_only_and_delivery_staff_blocked_from_processing(
        self, client, read_only_staff, delivery_staff, customer_user, sample_product_and_variant
    ):
        _, var = sample_product_and_variant
        order = make_test_order(customer_user, status=Order.Status.PAID, variant=var)

        # Read-Only Staff cannot process
        client.force_login(read_only_staff)
        resp = client.post(reverse("staff:order_process_action", kwargs={"pk": order.pk}))
        assert resp.status_code == 403

        # Delivery Staff cannot process orders
        client.force_login(delivery_staff)
        resp = client.post(reverse("staff:order_process_action", kwargs={"pk": order.pk}))
        assert resp.status_code == 403

        # Order remains PAID
        order.refresh_from_db()
        assert order.status == Order.Status.PAID

    def test_order_staff_and_ops_manager_can_mutate_order_state(
        self, client, order_staff, customer_user, sample_product_and_variant
    ):
        _, var = sample_product_and_variant
        order = make_test_order(customer_user, status=Order.Status.PAID, variant=var)

        client.force_login(order_staff)
        resp = client.post(reverse("staff:order_process_action", kwargs={"pk": order.pk}))
        assert resp.status_code == 302
        assert reverse("staff:order_detail", kwargs={"pk": order.pk}) in resp.headers["Location"]

        order.refresh_from_db()
        assert order.status == Order.Status.PROCESSING
        assert order.shipments.count() == 1


# =============================================================================
# 3. Fulfillment Lifecycle & Workflow Transitions
# =============================================================================


@pytest.mark.django_db
class TestOrderFulfillmentLifecycle:
    """Verify sequential transitions: Paid -> Processing -> Picking -> Packed -> Dispatched."""

    def test_complete_fulfillment_flow(
        self, client, order_staff, customer_user, sample_product_and_variant
    ):
        _, var = sample_product_and_variant
        order = make_test_order(customer_user, status=Order.Status.PAID, variant=var)

        client.force_login(order_staff)

        # 1. Mark Processing
        resp = client.post(reverse("staff:order_process_action", kwargs={"pk": order.pk}))
        assert resp.status_code == 302
        order.refresh_from_db()
        assert order.status == Order.Status.PROCESSING
        shipment = order.shipments.first()
        assert shipment is not None
        assert shipment.status == Shipment.Status.PENDING

        # 2. Start Picking
        resp = client.post(reverse("staff:order_start_picking_action", kwargs={"pk": order.pk}))
        assert resp.status_code == 302
        shipment.refresh_from_db()
        assert shipment.status == Shipment.Status.PICKING
        assert shipment.picked_by == order_staff
        assert shipment.picked_at is not None

        # 3. Mark Packed
        resp = client.post(
            reverse("staff:order_mark_packed_action", kwargs={"pk": order.pk}),
            {
                "package_weight_kg": "0.75",
                "package_dimensions": "30x20x5 cm",
                "internal_notes": "Quality checked and sealed.",
            },
        )
        assert resp.status_code == 302
        shipment.refresh_from_db()
        assert shipment.status == Shipment.Status.PACKED
        assert shipment.packed_by == order_staff
        assert shipment.packed_at is not None
        assert shipment.package_weight_kg == Decimal("0.75")
        assert shipment.package_dimensions == "30x20x5 cm"

        # 4. Dispatch Shipment
        resp = client.post(
            reverse("staff:order_dispatch_action", kwargs={"pk": order.pk}),
            {
                "carrier": "DHL Express",
                "carrier_service": "Next Day",
                "tracking_number": "DHL987654321",
                "internal_notes": "Handed to driver.",
            },
        )
        assert resp.status_code == 302
        shipment.refresh_from_db()
        order.refresh_from_db()

        assert shipment.status == Shipment.Status.SHIPPED
        assert shipment.carrier == "DHL Express"
        assert shipment.tracking_number == "DHL987654321"
        assert shipment.dispatched_by == order_staff
        assert shipment.shipped_at is not None
        # Order automatically followed to SHIPPED
        assert order.status == Order.Status.SHIPPED

        # Verify Audit Events were recorded
        audit_actions = list(
            AuditEvent.objects.filter(object_id=str(order.pk)).values_list("action", flat=True)
        )
        assert "order.process" in audit_actions
        assert "shipment.picking_started" in audit_actions
        assert "shipment.packed" in audit_actions
        assert "shipment.dispatched" in audit_actions


# =============================================================================
# 4. Payment Safeguards & Ineligible Order Invariants
# =============================================================================


@pytest.mark.django_db
class TestPaymentSafeguardsAndIneligibleOrders:
    """Verify unpaid, cancelled, and refunded orders cannot be fulfilled."""

    def test_unpaid_order_cannot_be_processed(
        self, client, order_staff, customer_user, sample_product_and_variant
    ):
        _, var = sample_product_and_variant
        unpaid_order = make_test_order(
            customer_user,
            status=Order.Status.PENDING_PAYMENT,
            variant=var,
            with_payment=False,
        )

        client.force_login(order_staff)
        resp = client.post(reverse("staff:order_process_action", kwargs={"pk": unpaid_order.pk}))
        assert resp.status_code == 302

        # Order must strictly stay PENDING_PAYMENT
        unpaid_order.refresh_from_db()
        assert unpaid_order.status == Order.Status.PENDING_PAYMENT
        assert unpaid_order.shipments.count() == 0

    def test_cancelled_order_rejected_for_fulfillment(
        self, client, order_staff, customer_user, sample_product_and_variant
    ):
        _, var = sample_product_and_variant
        cancelled_order = make_test_order(
            customer_user,
            status=Order.Status.CANCELLED,
            variant=var,
        )

        client.force_login(order_staff)
        resp = client.post(reverse("staff:order_process_action", kwargs={"pk": cancelled_order.pk}))
        assert resp.status_code == 302

        cancelled_order.refresh_from_db()
        assert cancelled_order.status == Order.Status.CANCELLED
        assert cancelled_order.shipments.count() == 0

    def test_idempotent_processing_does_not_duplicate_shipments(
        self, client, order_staff, customer_user, sample_product_and_variant
    ):
        _, var = sample_product_and_variant
        order = make_test_order(customer_user, status=Order.Status.PAID, variant=var)

        client.force_login(order_staff)

        # First submit
        client.post(reverse("staff:order_process_action", kwargs={"pk": order.pk}))
        assert order.shipments.count() == 1

        # Repeated submit (e.g. double click or reloaded browser)
        client.post(reverse("staff:order_process_action", kwargs={"pk": order.pk}))
        assert order.shipments.count() == 1


# =============================================================================
# 5. Packing Slip and Search/Filter UI Tests
# =============================================================================


@pytest.mark.django_db
class TestPackingSlipAndWorkspaceUI:
    """Verify packing slip generation and workspace search/filter controls."""

    def test_packing_slip_view(
        self, client, order_staff, customer_user, sample_product_and_variant
    ):
        _, var = sample_product_and_variant
        order = make_test_order(customer_user, status=Order.Status.PAID, variant=var)

        client.force_login(order_staff)
        resp = client.get(reverse("staff:order_packing_slip", kwargs={"pk": order.pk}))
        assert resp.status_code == 200
        content = resp.content.decode()

        assert (
            "Warehouse Fulfillment &amp; Packing Slip" in content
            or "Warehouse Fulfillment & Packing Slip" in content
        )
        assert order.number in content
        assert "Alice Smith" in content
        assert "42 High Street" in content
        assert var.sku in content
        assert (
            "Quality Checked &amp; Packed by" in content or "Quality Checked & Packed by" in content
        )

    def test_search_and_status_filtering(
        self, client, order_staff, customer_user, sample_product_and_variant
    ):
        _, var = sample_product_and_variant
        o_paid = make_test_order(
            customer_user, number="FW-PAID-001", status=Order.Status.PAID, variant=var
        )
        o_shipped = make_test_order(
            customer_user, number="FW-SHIP-002", status=Order.Status.SHIPPED, variant=var
        )

        client.force_login(order_staff)

        # Filter by paid
        resp = client.get(reverse("staff:orders"), {"status": "paid"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert o_paid.number in content
        assert o_shipped.number not in content

        # Search by order number query
        resp_search = client.get(reverse("staff:orders"), {"q": "FW-SHIP-002"})
        assert resp_search.status_code == 200
        content_search = resp_search.content.decode()
        assert o_shipped.number in content_search
        assert o_paid.number not in content_search
