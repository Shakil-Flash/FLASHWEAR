"""Automated tests for FLASHWEAR Phase 61: Delivery & Shipment Operations.

Covers:
1. Delivery Operations Workspace access & RBAC boundaries.
2. Insecure Direct Object Reference (IDOR) protection: delivery workers cannot view
   or mutate other workers' shipments.
3. Shipment assignment & reassignment by authorized managers.
4. Delivery status workflow transitions & validation guards.
5. Delivery attempts, failure reasons, rescheduling, and manager review flagging.
6. Proof of Delivery (POD) verification: OTP code hashing, rate-limiting, and recipient sign-off.
7. Tracking timeline & audit log persistence.
8. Staff dashboard delivery metrics and queue integration.
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
from apps.orders.models import (
    Order,
    OrderAddress,
    OrderItem,
    Shipment,
    ShipmentItem,
)
from apps.shop.models import Cart, CheckoutSession
from apps.staff.constants import ROLE_PRIMARY_GROUP, StaffRole
from apps.staff.dashboard_services import get_my_work_and_team_queues
from apps.staff.delivery_services import (
    generate_delivery_otp_action,
)

User = get_user_model()


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def owner_user(db):
    u = User.objects.create_superuser(
        email="owner.deliv@flashwear.com",
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
        email="ops.deliv@flashwear.com",
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
        email="processor.deliv@flashwear.com",
        password="ValidPassword123!",
        first_name="Order",
        last_name="Processor",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.ORDER_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def delivery_driver_1(db):
    u = User.objects.create_user(
        email="driver1.deliv@flashwear.com",
        password="ValidPassword123!",
        first_name="Dave",
        last_name="Driver",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.DELIVERY_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def delivery_driver_2(db):
    u = User.objects.create_user(
        email="driver2.deliv@flashwear.com",
        password="ValidPassword123!",
        first_name="Dan",
        last_name="Delivery",
    )
    g = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.DELIVERY_STAFF])
    u.groups.add(g)
    return u


@pytest.fixture
def support_staff(db):
    u = User.objects.create_user(
        email="support.deliv@flashwear.com",
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
        email="pm.deliv@flashwear.com",
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
        email="inv.deliv@flashwear.com",
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
        email="readonly.deliv@flashwear.com",
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
        email="shopper.deliv@flashwear.com",
        password="ValidPassword123!",
        first_name="Alice",
        last_name="Smith",
    )


@pytest.fixture
def sample_variant(db):
    cat = Category.objects.create(name="Tees", slug=f"tees-{uuid.uuid4().hex[:6]}")
    prod = Product.objects.create(
        name="Essential T-Shirt",
        slug=f"essential-tshirt-{uuid.uuid4().hex[:6]}",
        category=cat,
    )
    return ProductVariant.objects.create(
        product=prod,
        sku=f"TEE-BLK-{uuid.uuid4().hex[:6].upper()}",
        price=Decimal("25.00"),
    )


def make_shipment_fixture(
    customer_user,
    variant,
    status=Shipment.Status.SHIPPED,
    assigned_to=None,
    reference=None,
):
    """Helper to construct an order with address and associated shipment."""
    num = f"FW-{uuid.uuid4().hex[:8].upper()}"
    cart = Cart.objects.create(user=customer_user, status=Cart.Status.CONVERTED)
    addr, _ = Address.objects.get_or_create(
        user=customer_user,
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
        user=customer_user,
        cart=cart,
        shipping_address=addr,
        status=CheckoutSession.Status.CONVERTED,
    )
    order = Order.objects.create(
        user=customer_user,
        number=num,
        checkout=checkout,
        status=Order.Status.SHIPPED,
        currency="GBP",
        subtotal=Decimal("25.00"),
        shipping_amount=Decimal("0.00"),
        total=Decimal("25.00"),
        shipping_code="standard",
        shipping_name="Standard Tracked Delivery",
    )
    OrderAddress.objects.create(
        order=order,
        full_name=addr.full_name,
        phone=addr.phone,
        line1=addr.line1,
        city=addr.city,
        postal_code=addr.postal_code,
        country=addr.country,
    )
    order_item = OrderItem.objects.create(
        order=order,
        variant=variant,
        sku=variant.sku,
        product_name=variant.product.name,
        unit_price=Decimal("25.00"),
        quantity=1,
        line_total=Decimal("25.00"),
    )
    ref = reference or f"SHP-{num}-1"
    shipment = Shipment.objects.create(
        order=order,
        reference=ref,
        status=status,
        method="standard",
        carrier="FlashCourier",
        tracking_number=f"TRK-{uuid.uuid4().hex[:8].upper()}",
        assigned_to=assigned_to,
    )
    ShipmentItem.objects.create(shipment=shipment, order_item=order_item, quantity=1)
    return shipment


# =============================================================================
# 1. Delivery Workspace Access & Role Boundaries
# =============================================================================


@pytest.mark.django_db
class TestDeliveryAccessAndRoles:
    """Verify access controls for /staff/deliveries/ by role."""

    def test_anonymous_redirected_to_staff_login(self, client):
        resp = client.get(reverse("staff:deliveries"))
        assert resp.status_code == 302
        assert reverse("staff:login") in resp.headers["Location"]

    def test_customer_user_forbidden(self, client, customer_user):
        client.force_login(customer_user)
        resp = client.get(reverse("staff:deliveries"))
        assert resp.status_code == 403
        assert "Access Denied" in resp.content.decode()

    def test_product_manager_and_inventory_staff_denied_deliveries(
        self, client, product_manager, inventory_staff, customer_user, sample_variant
    ):
        # Product manager lacks staff.deliveries.view -> 403 Forbidden
        client.force_login(product_manager)
        resp = client.get(reverse("staff:deliveries"))
        assert resp.status_code == 403
        assert "staff.deliveries.view" in resp.content.decode()

        # Inventory staff has staff.deliveries.view -> 200 OK
        client.force_login(inventory_staff)
        resp = client.get(reverse("staff:deliveries"))
        assert resp.status_code == 200

        # But inventory staff lacks staff.deliveries.manage -> 403 Forbidden on action mutations
        s = make_shipment_fixture(customer_user, sample_variant, status=Shipment.Status.SHIPPED)
        resp_post = client.post(
            reverse("staff:delivery_status_action", kwargs={"pk": s.pk}),
            {"target_status": Shipment.Status.IN_TRANSIT},
        )
        assert resp_post.status_code == 403
        assert "staff.deliveries.manage" in resp_post.content.decode()

    def test_authorized_roles_can_access_deliveries(
        self,
        client,
        owner_user,
        ops_manager,
        order_staff,
        delivery_driver_1,
        support_staff,
        read_only_staff,
    ):
        for u in [
            owner_user,
            ops_manager,
            order_staff,
            delivery_driver_1,
            support_staff,
            read_only_staff,
        ]:
            client.force_login(u)
            resp = client.get(reverse("staff:deliveries"))
            assert resp.status_code == 200
            content = resp.content.decode()
            assert (
                "Delivery &amp; Shipment Operations" in content
                or "Delivery & Shipment Operations" in content
            )

    def test_pure_delivery_staff_only_sees_assigned_shipments(
        self, client, delivery_driver_1, delivery_driver_2, customer_user, sample_variant
    ):
        # Driver 1 has shipment 1
        s1 = make_shipment_fixture(
            customer_user, sample_variant, reference="SHP-DRV1-001", assigned_to=delivery_driver_1
        )
        # Driver 2 has shipment 2
        s2 = make_shipment_fixture(
            customer_user, sample_variant, reference="SHP-DRV2-002", assigned_to=delivery_driver_2
        )

        client.force_login(delivery_driver_1)
        resp = client.get(reverse("staff:deliveries"))
        assert resp.status_code == 200
        content = resp.content.decode()

        # Driver 1 sees their assigned shipment, but Driver 2's is omitted
        assert s1.reference in content
        assert s2.reference not in content


# =============================================================================
# 2. Insecure Direct Object Reference (IDOR) Protection
# =============================================================================


@pytest.mark.django_db
class TestIDORAndShipmentDetailAccess:
    """Verify delivery staff cannot view or alter parcels assigned to other workers."""

    def test_driver_can_view_assigned_shipment(
        self, client, delivery_driver_1, customer_user, sample_variant
    ):
        s = make_shipment_fixture(
            customer_user, sample_variant, reference="SHP-MINE-1", assigned_to=delivery_driver_1
        )
        client.force_login(delivery_driver_1)
        resp = client.get(reverse("staff:delivery_detail", kwargs={"pk": s.pk}))
        assert resp.status_code == 200
        assert s.reference in resp.content.decode()

    def test_driver_denied_other_workers_shipment(
        self, client, delivery_driver_1, delivery_driver_2, customer_user, sample_variant
    ):
        s_other = make_shipment_fixture(
            customer_user, sample_variant, reference="SHP-OTHER-2", assigned_to=delivery_driver_2
        )
        client.force_login(delivery_driver_1)
        resp = client.get(reverse("staff:delivery_detail", kwargs={"pk": s_other.pk}))
        assert resp.status_code == 403
        assert "Access Denied" in resp.content.decode()

    def test_operations_manager_and_owner_can_view_any_shipment(
        self, client, ops_manager, owner_user, delivery_driver_1, customer_user, sample_variant
    ):
        s = make_shipment_fixture(
            customer_user, sample_variant, reference="SHP-ANY-3", assigned_to=delivery_driver_1
        )
        for mgr in [ops_manager, owner_user]:
            client.force_login(mgr)
            resp = client.get(reverse("staff:delivery_detail", kwargs={"pk": s.pk}))
            assert resp.status_code == 200
            assert s.reference in resp.content.decode()

    def test_nonexistent_shipment_redirects_safely(self, client, ops_manager):
        client.force_login(ops_manager)
        resp = client.get(reverse("staff:delivery_detail", kwargs={"pk": 999999}))
        assert resp.status_code == 302
        assert reverse("staff:deliveries") in resp.headers["Location"]


# =============================================================================
# 3. Shipment Assignment & Manager Permissions
# =============================================================================


@pytest.mark.django_db
class TestShipmentAssignmentWorkflow:
    """Verify assignment rules, role checks, and audit trail."""

    def test_manager_can_assign_and_reassign_shipment(
        self,
        client,
        ops_manager,
        delivery_driver_1,
        delivery_driver_2,
        customer_user,
        sample_variant,
    ):
        shipment = make_shipment_fixture(
            customer_user, sample_variant, status=Shipment.Status.SHIPPED, assigned_to=None
        )

        client.force_login(ops_manager)

        # 1. First assignment to Driver 1
        resp = client.post(
            reverse("staff:delivery_assign_action", kwargs={"pk": shipment.pk}),
            {"assigned_to": str(delivery_driver_1.pk), "assignment_note": "Morning delivery route"},
        )
        assert resp.status_code == 302
        shipment.refresh_from_db()
        assert shipment.assigned_to == delivery_driver_1
        assert shipment.assigned_by == ops_manager
        assert shipment.assigned_at is not None

        # 2. Reassignment to Driver 2
        resp2 = client.post(
            reverse("staff:delivery_assign_action", kwargs={"pk": shipment.pk}),
            {
                "assigned_to": str(delivery_driver_2.pk),
                "assignment_note": "Reassigned due to vehicle issue",
            },
        )
        assert resp2.status_code == 302
        shipment.refresh_from_db()
        assert shipment.assigned_to == delivery_driver_2

        # 3. Verify Audit Trail
        audit_events = list(
            AuditEvent.objects.filter(object_id=str(shipment.pk)).values_list("action", flat=True)
        )
        assert "shipment.assigned" in audit_events
        assert "shipment.reassigned" in audit_events

    def test_delivery_worker_and_readonly_cannot_assign_shipments(
        self, client, delivery_driver_1, read_only_staff, customer_user, sample_variant
    ):
        shipment = make_shipment_fixture(customer_user, sample_variant, assigned_to=None)

        for u in [delivery_driver_1, read_only_staff]:
            client.force_login(u)
            resp = client.post(
                reverse("staff:delivery_assign_action", kwargs={"pk": shipment.pk}),
                {"assigned_to": str(delivery_driver_1.pk)},
            )
            assert resp.status_code == 403

        shipment.refresh_from_db()
        assert shipment.assigned_to is None

    def test_cannot_assign_completed_or_cancelled_shipment(
        self, client, ops_manager, delivery_driver_1, customer_user, sample_variant
    ):
        s_delivered = make_shipment_fixture(
            customer_user, sample_variant, status=Shipment.Status.DELIVERED
        )
        client.force_login(ops_manager)
        resp = client.post(
            reverse("staff:delivery_assign_action", kwargs={"pk": s_delivered.pk}),
            {"assigned_to": str(delivery_driver_1.pk)},
        )
        assert resp.status_code == 302
        s_delivered.refresh_from_db()
        assert s_delivered.assigned_to is None


# =============================================================================
# 4. Delivery Status Workflow & Synchronization
# =============================================================================


@pytest.mark.django_db
class TestDeliveryStatusWorkflow:
    """Verify transit status updates: Shipped -> In Transit -> Out for Delivery."""

    def test_status_update_progression(
        self, client, delivery_driver_1, customer_user, sample_variant
    ):
        shipment = make_shipment_fixture(
            customer_user,
            sample_variant,
            status=Shipment.Status.SHIPPED,
            assigned_to=delivery_driver_1,
        )

        client.force_login(delivery_driver_1)

        # 1. Update to IN_TRANSIT
        resp = client.post(
            reverse("staff:delivery_status_action", kwargs={"pk": shipment.pk}),
            {
                "target_status": Shipment.Status.IN_TRANSIT,
                "location": "North London Sorting Depot",
                "note": "Sorted and departing depot",
            },
        )
        assert resp.status_code == 302
        shipment.refresh_from_db()
        assert shipment.status == Shipment.Status.IN_TRANSIT

        # 2. Update to OUT_FOR_DELIVERY (automatically generates OTP)
        resp2 = client.post(
            reverse("staff:delivery_status_action", kwargs={"pk": shipment.pk}),
            {
                "target_status": Shipment.Status.OUT_FOR_DELIVERY,
                "location": "High Street Delivery Van",
                "note": "Loaded for delivery",
            },
        )
        assert resp2.status_code == 302
        shipment.refresh_from_db()
        assert shipment.status == Shipment.Status.OUT_FOR_DELIVERY
        # OTP was generated
        assert shipment.pod_otp_hash != ""

    def test_invalid_status_transition_rejected(
        self, client, delivery_driver_1, customer_user, sample_variant
    ):
        shipment = make_shipment_fixture(
            customer_user,
            sample_variant,
            status=Shipment.Status.DELIVERED,
            assigned_to=delivery_driver_1,
        )

        client.force_login(delivery_driver_1)
        resp = client.post(
            reverse("staff:delivery_status_action", kwargs={"pk": shipment.pk}),
            {"target_status": Shipment.Status.SHIPPED},
        )
        assert resp.status_code == 302
        shipment.refresh_from_db()
        # Delivered shipments cannot regress
        assert shipment.status == Shipment.Status.DELIVERED

    def test_unassigned_worker_cannot_mutate_shipment(
        self, client, delivery_driver_1, delivery_driver_2, customer_user, sample_variant
    ):
        shipment = make_shipment_fixture(
            customer_user,
            sample_variant,
            status=Shipment.Status.SHIPPED,
            assigned_to=delivery_driver_2,
        )

        # Driver 1 attempts to change Driver 2's shipment
        client.force_login(delivery_driver_1)
        resp = client.post(
            reverse("staff:delivery_status_action", kwargs={"pk": shipment.pk}),
            {"target_status": Shipment.Status.IN_TRANSIT},
        )
        assert resp.status_code == 403
        shipment.refresh_from_db()
        assert shipment.status == Shipment.Status.SHIPPED


# =============================================================================
# 5. Delivery Attempts and Exceptions
# =============================================================================


@pytest.mark.django_db
class TestDeliveryAttemptsAndExceptions:
    """Verify failed attempts, failure reasons, rescheduling, and manager review flags."""

    def test_record_failed_delivery_attempt(
        self, client, delivery_driver_1, customer_user, sample_variant
    ):
        shipment = make_shipment_fixture(
            customer_user,
            sample_variant,
            status=Shipment.Status.OUT_FOR_DELIVERY,
            assigned_to=delivery_driver_1,
        )

        client.force_login(delivery_driver_1)

        # 1. First attempt fails: recipient unavailable
        resp = client.post(
            reverse("staff:delivery_attempt_action", kwargs={"pk": shipment.pk}),
            {
                "failure_reason": "recipient_unavailable",
                "location": "42 High Street Doorstep",
                "next_attempt_date": "2026-10-15",
                "note": "Intercom unanswered, left delivery card #101.",
            },
        )
        assert resp.status_code == 302
        shipment.refresh_from_db()
        assert shipment.status == Shipment.Status.ATTEMPT_FAILED
        assert shipment.delivery_attempts_count == 1
        assert str(shipment.next_attempt_date) == "2026-10-15"
        assert shipment.requires_manager_review is False

        # 2. Second attempt fails -> automatically triggers manager review
        resp2 = client.post(
            reverse("staff:delivery_attempt_action", kwargs={"pk": shipment.pk}),
            {
                "failure_reason": "address_incomplete",
                "location": "Building Entrance",
                "note": "Flat number missing from building directory.",
            },
        )
        assert resp2.status_code == 302
        shipment.refresh_from_db()
        assert shipment.delivery_attempts_count == 2
        assert shipment.requires_manager_review is True

        # Check Audit row
        audit_events = list(
            AuditEvent.objects.filter(object_id=str(shipment.pk)).values_list("action", flat=True)
        )
        assert "shipment.attempt" in audit_events


# =============================================================================
# 6. Proof of Delivery (POD) Workflows
# =============================================================================


@pytest.mark.django_db
class TestProofOfDeliveryWorkflows:
    """Verify OTP verification, attempt rate-limiting, and recipient sign-off."""

    def test_successful_otp_proof_of_delivery(
        self, client, delivery_driver_1, customer_user, sample_variant
    ):
        shipment = make_shipment_fixture(
            customer_user,
            sample_variant,
            status=Shipment.Status.OUT_FOR_DELIVERY,
            assigned_to=delivery_driver_1,
        )

        # Generate OTP
        _, raw_otp = generate_delivery_otp_action(shipment.pk, actor=delivery_driver_1)
        assert len(raw_otp) == 6

        client.force_login(delivery_driver_1)

        # Submit valid OTP
        resp = client.post(
            reverse("staff:delivery_pod_action", kwargs={"pk": shipment.pk}),
            {
                "pod_type": "otp",
                "otp_code": raw_otp,
                "notes": "Handed to customer outside front door.",
            },
        )
        assert resp.status_code == 302
        shipment.refresh_from_db()

        assert shipment.status == Shipment.Status.DELIVERED
        assert shipment.pod_type == "otp"
        assert shipment.pod_verified_at is not None
        assert shipment.delivered_at is not None

        # Order status synchronized to DELIVERED
        order = shipment.order
        order.refresh_from_db()
        assert order.status == Order.Status.DELIVERED

    def test_invalid_otp_rejected_and_locked_out_after_five_attempts(
        self, client, delivery_driver_1, customer_user, sample_variant
    ):
        shipment = make_shipment_fixture(
            customer_user,
            sample_variant,
            status=Shipment.Status.OUT_FOR_DELIVERY,
            assigned_to=delivery_driver_1,
        )
        generate_delivery_otp_action(shipment.pk, actor=delivery_driver_1)

        client.force_login(delivery_driver_1)

        # 4 wrong attempts
        for _ in range(4):
            client.post(
                reverse("staff:delivery_pod_action", kwargs={"pk": shipment.pk}),
                {"pod_type": "otp", "otp_code": "000000"},
            )
            shipment.refresh_from_db()
            assert shipment.status == Shipment.Status.OUT_FOR_DELIVERY

        assert shipment.pod_otp_attempts == 4
        assert shipment.requires_manager_review is False

        # 5th wrong attempt triggers lockout and manager review
        client.post(
            reverse("staff:delivery_pod_action", kwargs={"pk": shipment.pk}),
            {"pod_type": "otp", "otp_code": "000000"},
        )
        shipment.refresh_from_db()
        assert shipment.pod_otp_attempts == 5
        assert shipment.requires_manager_review is True
        assert shipment.status == Shipment.Status.OUT_FOR_DELIVERY

    def test_recipient_signoff_proof_of_delivery(
        self, client, delivery_driver_1, customer_user, sample_variant
    ):
        shipment = make_shipment_fixture(
            customer_user,
            sample_variant,
            status=Shipment.Status.OUT_FOR_DELIVERY,
            assigned_to=delivery_driver_1,
        )

        client.force_login(delivery_driver_1)

        resp = client.post(
            reverse("staff:delivery_pod_action", kwargs={"pk": shipment.pk}),
            {
                "pod_type": "recipient_signoff",
                "recipient_name": "Bob Smith (Partner)",
                "notes": "Signed on parcel receiver form.",
            },
        )
        assert resp.status_code == 302
        shipment.refresh_from_db()

        assert shipment.status == Shipment.Status.DELIVERED
        assert shipment.pod_type == "recipient_signoff"
        assert shipment.pod_recipient_name == "Bob Smith (Partner)"
        assert shipment.pod_verified_at is not None


# =============================================================================
# 7. Dashboard Queue Integration
# =============================================================================


@pytest.mark.django_db
class TestDashboardDeliveryIntegration:
    """Verify delivery assignments and alerts surface on the staff dashboard."""

    def test_driver_dashboard_shows_assigned_parcels(
        self, delivery_driver_1, customer_user, sample_variant
    ):
        s_assigned = make_shipment_fixture(
            customer_user,
            sample_variant,
            status=Shipment.Status.OUT_FOR_DELIVERY,
            assigned_to=delivery_driver_1,
            reference="SHP-DASH-1",
        )

        queues = get_my_work_and_team_queues(delivery_driver_1)
        my_work_titles = [item["title"] for item in queues["my_work"]]

        assert s_assigned.reference in str(my_work_titles)

    def test_dashboard_surfaces_manager_review_alerts(
        self, ops_manager, customer_user, sample_variant
    ):
        s_flagged = make_shipment_fixture(
            customer_user,
            sample_variant,
            status=Shipment.Status.ATTEMPT_FAILED,
            reference="SHP-FLAGGED-9",
        )
        s_flagged.requires_manager_review = True
        s_flagged.save()

        queues = get_my_work_and_team_queues(ops_manager)
        alert_titles = [item["title"] for item in queues["urgent_alerts"]]

        assert s_flagged.reference in str(alert_titles)
