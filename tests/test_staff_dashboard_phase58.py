"""Automated Test Suite for Phase 58:
Unified Staff Dashboard & Daily Operations Center.
"""

import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts.models import Address
from apps.backoffice.models import AuditEvent
from apps.catalog.models import Category, Product, ProductVariant
from apps.inventory.models import Stock
from apps.orders.models import Order, OrderItem, ReturnRequest, Shipment
from apps.shop.models import Cart, CheckoutSession
from apps.staff.constants import ROLE_PRIMARY_GROUP, StaffRole
from apps.staff.dashboard_services import (
    get_daily_operations_summary,
    get_my_work_and_team_queues,
    get_quick_actions,
    get_recent_activity_feed,
    parse_date_range,
)
from apps.staff.permissions import ensure_staff_groups
from apps.support.models import SupportTicket

User = get_user_model()


def make_test_order(user, number=None, status=Order.Status.PAID, total=Decimal("50.00")):
    if not number:
        number = f"ORD-{uuid.uuid4().hex[:8].upper()}"
    cart = Cart.objects.create(user=user, status=Cart.Status.CONVERTED)
    addr, _ = Address.objects.get_or_create(
        user=user,
        defaults={
            "full_name": "Test Customer",
            "phone": "+1234567890",
            "line1": "123 Fashion Way",
            "city": "London",
            "postal_code": "SW1A 1AA",
            "country": "GB",
        },
    )
    checkout = CheckoutSession.objects.create(
        user=user,
        cart=cart,
        shipping_address=addr,
        status=CheckoutSession.Status.CONVERTED,
    )
    return Order.objects.create(
        user=user,
        number=number,
        checkout=checkout,
        status=status,
        currency="USD",
        subtotal=total,
        shipping_amount=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total=total,
        shipping_code="standard",
        shipping_name="Standard Shipping",
    )


@pytest.fixture(autouse=True)
def setup_groups(db):
    """Ensure all required staff groups exist before each test."""
    ensure_staff_groups()


@pytest.fixture
def owner_user(db):
    u = User.objects.create_user(
        email="owner.dashboard@flashwear.com",
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
        email="ops.dashboard@flashwear.com",
        password="ValidPassword123!",
        first_name="Ops",
        last_name="Manager",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.OPERATIONS_MANAGER])
    u.groups.add(g)
    return u


@pytest.fixture
def product_manager(db):
    u = User.objects.create_user(
        email="pm.dashboard@flashwear.com",
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
        email="inv.dashboard@flashwear.com",
        password="ValidPassword123!",
        first_name="Inventory",
        last_name="Staff",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.INVENTORY_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def order_staff(db):
    u = User.objects.create_user(
        email="orders.dashboard@flashwear.com",
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
        email="delivery.dashboard@flashwear.com",
        password="ValidPassword123!",
        first_name="Delivery",
        last_name="Staff",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.DELIVERY_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def support_staff(db):
    u = User.objects.create_user(
        email="support.dashboard@flashwear.com",
        password="ValidPassword123!",
        first_name="Support",
        last_name="Hero",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.SUPPORT_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def read_only_staff(db):
    u = User.objects.create_user(
        email="readonly.dashboard@flashwear.com",
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
        email="shopper.dashboard@flashwear.com",
        password="ValidPassword123!",
        first_name="Plain",
        last_name="Shopper",
    )


@pytest.fixture
def sample_category(db):
    return Category.objects.create(name="T-Shirts", slug="t-shirts")


@pytest.mark.django_db
class TestDashboardAccessAndSecurity:
    """Verify endpoint authentication, access rejection, and session validation."""

    def test_anonymous_redirected_to_staff_login(self, client):
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 302
        assert reverse("staff:login") in resp.headers["Location"]
        assert "next=/staff/" in resp.headers["Location"]

    def test_customer_user_forbidden_on_dashboard(self, client, customer_user):
        client.force_login(customer_user)
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 403
        assert "Access Denied" in resp.content.decode()

    def test_inactive_staff_member_redirected(self, client, ops_manager):
        ops_manager.is_active = False
        ops_manager.save()
        client.force_login(ops_manager)

        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 302
        assert reverse("staff:login") in resp.headers["Location"]

    def test_authorized_staff_can_access_dashboard(self, client, ops_manager):
        client.force_login(ops_manager)
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Daily Operations Center" in content
        assert "Welcome back" in content
        assert ops_manager.first_name in content


@pytest.mark.django_db
class TestRoleSpecificSummariesAndVisibility:
    """Verify each role only sees summaries and cards matching its capabilities."""

    def test_product_manager_sees_catalog_and_inventory(
        self, client, product_manager, sample_category
    ):
        Product.objects.create(
            name="Alpha Tee",
            slug="alpha-tee",
            category=sample_category,
            status=Product.Status.ACTIVE,
        )
        Product.objects.create(
            name="Beta Tee",
            slug="beta-tee",
            category=sample_category,
            status=Product.Status.DRAFT,
        )

        client.force_login(product_manager)
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 200
        content = resp.content.decode()

        assert "Catalogue & Items" in content
        assert "active products" in content
        # Disallowed sections should be absent
        assert "Order Processing" not in content
        assert "Shipping & Deliveries" not in content
        assert "Customer Support Desk" not in content
        assert "Staff Accounts & RBAC" not in content

    def test_inventory_staff_sees_inventory_and_deliveries(
        self, client, inventory_staff, sample_category
    ):
        prod = Product.objects.create(name="Gamma Tee", slug="gamma-tee", category=sample_category)
        var = ProductVariant.objects.create(product=prod, sku="GAMMA-BLK-M", price=Decimal("25.00"))
        Stock.objects.create(variant=var, on_hand=2, low_stock_threshold=5)

        client.force_login(inventory_staff)
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 200
        content = resp.content.decode()

        assert "Inventory Health" in content
        assert "at risk items" in content
        assert "Order Processing" not in content
        assert "Customer Support Desk" not in content
        assert "Staff Accounts & RBAC" not in content

    def test_order_staff_sees_orders_and_returns(self, client, order_staff, customer_user):
        make_test_order(
            user=customer_user,
            number="FW-ORD-001",
            status=Order.Status.PAID,
            total=Decimal("50.00"),
        )
        make_test_order(
            user=customer_user,
            number="FW-ORD-002",
            status=Order.Status.PROCESSING,
            total=Decimal("75.00"),
        )

        client.force_login(order_staff)
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 200
        content = resp.content.decode()

        assert "Order Processing" in content
        assert "in queue" in content
        assert "Catalogue & Items" not in content
        assert "Customer Support Desk" not in content
        assert "Staff Accounts & RBAC" not in content

    def test_delivery_staff_sees_deliveries_and_orders(self, client, delivery_staff, customer_user):
        ord_obj = make_test_order(
            user=customer_user,
            number="FW-ORD-003",
            status=Order.Status.SHIPPED,
            total=Decimal("100.00"),
        )
        Shipment.objects.create(
            order=ord_obj,
            reference="SHP-001",
            status=Shipment.Status.IN_TRANSIT,
        )

        client.force_login(delivery_staff)
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 200
        content = resp.content.decode()

        assert "Shipping & Deliveries" in content
        assert "in transit" in content
        assert "Catalogue & Items" not in content
        assert "Inventory Health" not in content
        assert "Customer Support Desk" not in content
        assert "Staff Accounts & RBAC" not in content

    def test_support_staff_sees_support_and_deliveries(self, client, support_staff, customer_user):
        SupportTicket.objects.create(
            customer=customer_user,
            number="TCK-100",
            subject="Where is my order?",
            status=SupportTicket.Status.OPEN,
            priority=SupportTicket.Priority.URGENT,
        )

        client.force_login(support_staff)
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 200
        content = resp.content.decode()

        assert "Customer Support Desk" in content
        assert "active tickets" in content
        assert "Catalogue & Items" not in content
        assert "Inventory Health" not in content
        assert "Staff Accounts & RBAC" not in content

    def test_operations_manager_and_owner_see_all(self, client, ops_manager, owner_user):
        for u in [ops_manager, owner_user]:
            client.force_login(u)
            resp = client.get(reverse("staff:dashboard"))
            assert resp.status_code == 200
            content = resp.content.decode()
            assert "Catalogue & Items" in content
            assert "Inventory Health" in content
            assert "Order Processing" in content
            assert "Shipping & Deliveries" in content
            assert "Customer Support Desk" in content
            assert "Staff Accounts & RBAC" in content


@pytest.mark.django_db
class TestAccurateAggregationsAndDateRanges:
    """Verify domain aggregations are mathematically accurate and respect date boundaries."""

    def test_date_range_parser(self):
        start_none, _ = parse_date_range("all")
        assert start_none is None

        start_today, end_today = parse_date_range("today")
        assert start_today is not None
        assert (end_today - start_today).total_seconds() <= 86400

        start_7d, _ = parse_date_range("7d")
        assert start_7d is not None

        start_30d, _ = parse_date_range("30d")
        assert start_30d is not None

    def test_aggregations_distinguish_actionable_from_total(
        self, ops_manager, customer_user, sample_category
    ):
        # Create 1 draft and 2 active products
        Product.objects.create(
            name="P1", slug="p1", category=sample_category, status=Product.Status.DRAFT
        )
        Product.objects.create(
            name="P2", slug="p2", category=sample_category, status=Product.Status.ACTIVE
        )
        Product.objects.create(
            name="P3", slug="p3", category=sample_category, status=Product.Status.ACTIVE
        )

        # Create 1 paid order (ready), 1 processing (packing), 1 shipped
        make_test_order(
            user=customer_user, number="O1", status=Order.Status.PAID, total=Decimal("10.00")
        )
        make_test_order(
            user=customer_user, number="O2", status=Order.Status.PROCESSING, total=Decimal("20.00")
        )
        make_test_order(
            user=customer_user, number="O3", status=Order.Status.SHIPPED, total=Decimal("30.00")
        )

        # Create return request
        ord_obj = Order.objects.first()
        ReturnRequest.objects.create(
            order=ord_obj,
            user=customer_user,
            number="RET-01",
            status=ReturnRequest.Status.REQUESTED,
        )

        summary = get_daily_operations_summary(ops_manager, range_key="all")

        assert summary["products"]["total"] == 3
        assert summary["products"]["published"] == 2
        assert summary["products"]["draft"] == 1

        assert summary["orders"]["total"] == 3
        assert summary["orders"]["awaiting_processing"] == 1
        assert summary["orders"]["processing"] == 1
        assert summary["orders"]["actionable_queue"] == 2  # paid + processing

        assert summary["returns"]["pending_review"] == 1

    def test_orders_with_multiple_items_are_not_double_counted(
        self, ops_manager, customer_user, sample_category
    ):
        prod1 = Product.objects.create(
            name="Multi Item 1", slug="multi-item-1", category=sample_category
        )
        prod2 = Product.objects.create(
            name="Multi Item 2", slug="multi-item-2", category=sample_category
        )
        v1 = ProductVariant.objects.create(product=prod1, sku="M1", price=Decimal("15.00"))
        v2 = ProductVariant.objects.create(product=prod2, sku="M2", price=Decimal("20.00"))

        ord_obj = make_test_order(
            user=customer_user, number="O-MULTI", status=Order.Status.PAID, total=Decimal("50.00")
        )
        OrderItem.objects.create(
            order=ord_obj,
            variant=v1,
            sku="M1",
            product_name="P",
            quantity=2,
            unit_price=Decimal("15.00"),
            line_total=Decimal("30.00"),
        )
        OrderItem.objects.create(
            order=ord_obj,
            variant=v2,
            sku="M2",
            product_name="P",
            quantity=1,
            unit_price=Decimal("20.00"),
            line_total=Decimal("20.00"),
        )

        summary = get_daily_operations_summary(ops_manager, range_key="all")
        # Ensure count is 1, not 2
        assert summary["orders"]["total"] == 1
        assert summary["orders"]["awaiting_processing"] == 1


@pytest.mark.django_db
class TestMyWorkAndTeamQueues:
    """Verify my work shows assigned items and team queues show unassigned items."""

    def test_support_ticket_assignment_filtering(self, support_staff, ops_manager, customer_user):
        # Assigned to support_staff
        t1 = SupportTicket.objects.create(
            customer=customer_user,
            number="TCK-MY-1",
            subject="My assigned ticket",
            status=SupportTicket.Status.IN_PROGRESS,
            assigned_to=support_staff,
            priority=SupportTicket.Priority.HIGH,
        )
        # Assigned to ops_manager
        SupportTicket.objects.create(
            customer=customer_user,
            number="TCK-OTHER-1",
            subject="Other assigned ticket",
            status=SupportTicket.Status.OPEN,
            assigned_to=ops_manager,
        )
        # Unassigned ticket
        t3 = SupportTicket.objects.create(
            customer=customer_user,
            number="TCK-UNASSIGNED-1",
            subject="Queue ticket",
            status=SupportTicket.Status.OPEN,
            assigned_to=None,
        )

        queues = get_my_work_and_team_queues(support_staff)
        my_work_titles = [item["title"] for item in queues["my_work"]]
        team_titles = [item["title"] for item in queues["team_queues"]]

        assert f"Ticket #{t1.number}: {t1.subject}" in my_work_titles
        assert "TCK-OTHER-1" not in str(my_work_titles)
        assert f"Ticket #{t3.number}: {t3.subject}" in team_titles

    def test_shipment_assignment_and_exceptions(self, delivery_staff, customer_user):
        ord_obj = make_test_order(
            user=customer_user,
            number="ORD-SHP-1",
            status=Order.Status.PROCESSING,
            total=Decimal("50.00"),
        )

        # Assigned shipment
        s_my = Shipment.objects.create(
            order=ord_obj,
            reference="SHP-MY-1",
            status=Shipment.Status.PICKING,
            picked_by=delivery_staff,
        )
        # Unassigned shipment
        s_unassigned = Shipment.objects.create(
            order=ord_obj,
            reference="SHP-UN-1",
            status=Shipment.Status.PENDING,
            picked_by=None,
        )
        # Exception alert shipment
        s_exc = Shipment.objects.create(
            order=ord_obj,
            reference="SHP-EXC-1",
            status=Shipment.Status.EXCEPTION,
        )

        queues = get_my_work_and_team_queues(delivery_staff)
        my_work_refs = [item["title"] for item in queues["my_work"]]
        team_refs = [item["title"] for item in queues["team_queues"]]
        alert_titles = [item["title"] for item in queues["urgent_alerts"]]

        assert s_my.reference in str(my_work_refs)
        assert s_unassigned.reference in str(team_refs)
        assert s_exc.reference in str(alert_titles)


@pytest.mark.django_db
class TestRecentActivityFeedScopeEnforcement:
    """Verify audit activity feed restricts records to user's authorized domains."""

    def test_audit_feed_domain_scoping(self, product_manager, support_staff, owner_user):
        # Create events in catalog and support domains
        AuditEvent.objects.create(
            actor=product_manager,
            domain="catalog",
            action="product.publish",
            object_repr="Product Alpha",
        )
        AuditEvent.objects.create(
            actor=support_staff,
            domain="support",
            action="ticket.resolve",
            object_repr="Ticket #123",
        )
        AuditEvent.objects.create(
            actor=owner_user,
            domain="staff",
            action="staff.role.assign",
            object_repr="Employee Beta",
        )

        # Product Manager only sees catalog events
        pm_feed = get_recent_activity_feed(product_manager)
        pm_domains = [ev["domain"].lower() for ev in pm_feed]
        assert "catalog" in pm_domains
        assert "support" not in pm_domains
        assert "staff" not in pm_domains

        # Support Staff only sees support events
        supp_feed = get_recent_activity_feed(support_staff)
        supp_domains = [ev["domain"].lower() for ev in supp_feed]
        assert "support" in supp_domains
        assert "catalog" not in supp_domains

        # Owner sees all domains
        owner_feed = get_recent_activity_feed(owner_user)
        owner_domains = [ev["domain"].lower() for ev in owner_feed]
        assert "catalog" in owner_domains
        assert "support" in owner_domains
        assert "staff" in owner_domains


@pytest.mark.django_db
class TestQuickActionsAndModuleDrillDown:
    """Verify quick actions and filtered module views."""

    def test_quick_actions_scoped_by_permissions(
        self, product_manager, order_staff, read_only_staff
    ):
        pm_actions = [a["key"] for a in get_quick_actions(product_manager)]
        assert "products_drafts" in pm_actions
        assert "orders_queue" not in pm_actions

        order_actions = [a["key"] for a in get_quick_actions(order_staff)]
        assert "orders_queue" in order_actions
        assert "returns_pending" in order_actions
        assert "products_drafts" not in order_actions

        ro_actions = [a["key"] for a in get_quick_actions(read_only_staff)]
        assert "staff_invite" not in ro_actions

    def test_module_views_filter_by_query_param(self, client, ops_manager, sample_category):
        client.force_login(ops_manager)

        # Products filter
        p_draft = Product.objects.create(
            name="Draft P", slug="draft-p", category=sample_category, status=Product.Status.DRAFT
        )
        p_active = Product.objects.create(
            name="Active P", slug="active-p", category=sample_category, status=Product.Status.ACTIVE
        )

        resp = client.get(reverse("staff:products"), {"status": "draft"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert p_draft.name in content
        assert p_active.name not in content
        assert "Filter: draft" in content


@pytest.mark.django_db
class TestEmptyDatabaseResilience:
    """Verify dashboard renders gracefully with empty database."""

    def test_empty_db_renders_zero_counts_without_500(self, client, ops_manager):
        client.force_login(ops_manager)
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Daily Operations Center" in content
        assert "You&#39;re all caught up!" in content or "You're all caught up!" in content
        assert "Team queues are clear!" in content
