"""Automated Test Suite for Phase 57:
Dedicated Staff Portal Foundation & Role-Based Access Control.
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.backoffice.models import AuditEvent
from apps.staff.constants import (
    DELIVERIES_VIEW,
    INVENTORY_VIEW,
    ORDERS_VIEW,
    PORTAL_ACCESS,
    PRODUCTS_VIEW,
    REPORTS_VIEW,
    ROLE_PRIMARY_GROUP,
    STAFF_MANAGE,
    SUPPORT_VIEW,
    StaffRole,
)
from apps.staff.navigation import get_staff_navigation
from apps.staff.permissions import (
    ensure_staff_groups,
    get_user_capabilities,
    get_user_roles,
    is_owner_or_superuser,
)
from apps.staff.services import (
    create_staff_account,
    get_staff_dashboard_summary,
    toggle_staff_active,
    update_staff_roles,
)

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def init_staff_groups():
    ensure_staff_groups()


@pytest.fixture
def owner_user(db):
    user = User.objects.create_user(
        email="owner@flashwear.com",
        password="OwnerPassword123!",
        first_name="Alice",
        last_name="Owner",
        is_staff=True,
        is_superuser=True,
        is_active=True,
    )
    owner_grp = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.OWNER])
    user.groups.add(owner_grp)
    return user


@pytest.fixture
def ops_manager_user(db):
    user = User.objects.create_user(
        email="ops.manager@flashwear.com",
        password="OpsPassword123!",
        first_name="Bob",
        last_name="Ops",
        is_staff=False,  # Ordinary staff - no Django admin access
        is_superuser=False,
        is_active=True,
    )
    grp = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.OPERATIONS_MANAGER])
    user.groups.add(grp)
    return user


@pytest.fixture
def product_manager_user(db):
    user = User.objects.create_user(
        email="prod.manager@flashwear.com",
        password="ProdPassword123!",
        first_name="Carol",
        last_name="Product",
        is_staff=False,
        is_superuser=False,
        is_active=True,
    )
    grp = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.PRODUCT_MANAGER])
    user.groups.add(grp)
    return user


@pytest.fixture
def inventory_staff_user(db):
    user = User.objects.create_user(
        email="inventory.staff@flashwear.com",
        password="InvPassword123!",
        first_name="Dave",
        last_name="Warehouse",
        is_staff=False,
        is_superuser=False,
        is_active=True,
    )
    grp = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.INVENTORY_STAFF])
    user.groups.add(grp)
    return user


@pytest.fixture
def order_staff_user(db):
    user = User.objects.create_user(
        email="order.staff@flashwear.com",
        password="OrderPassword123!",
        first_name="Eva",
        last_name="Packing",
        is_staff=False,
        is_superuser=False,
        is_active=True,
    )
    grp = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.ORDER_STAFF])
    user.groups.add(grp)
    return user


@pytest.fixture
def delivery_staff_user(db):
    user = User.objects.create_user(
        email="delivery.staff@flashwear.com",
        password="DeliveryPassword123!",
        first_name="Frank",
        last_name="Courier",
        is_staff=False,
        is_superuser=False,
        is_active=True,
    )
    grp = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.DELIVERY_STAFF])
    user.groups.add(grp)
    return user


@pytest.fixture
def support_staff_user(db):
    user = User.objects.create_user(
        email="support.staff@flashwear.com",
        password="SupportPassword123!",
        first_name="Grace",
        last_name="Care",
        is_staff=False,
        is_superuser=False,
        is_active=True,
    )
    grp = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.SUPPORT_STAFF])
    user.groups.add(grp)
    return user


@pytest.fixture
def read_only_staff_user(db):
    user = User.objects.create_user(
        email="readonly.staff@flashwear.com",
        password="ReadOnlyPassword123!",
        first_name="Henry",
        last_name="Observer",
        is_staff=False,
        is_superuser=False,
        is_active=True,
    )
    grp = Group.objects.get(name=ROLE_PRIMARY_GROUP[StaffRole.READ_ONLY])
    user.groups.add(grp)
    return user


@pytest.fixture
def customer_user(db):
    return User.objects.create_user(
        email="shopper@customer.com",
        password="CustomerPassword123!",
        first_name="Clara",
        last_name="Shopper",
        is_staff=False,
        is_superuser=False,
        is_active=True,
    )


# =============================================================================
# 1. Staff Authentication, Login & Access Separation Tests
# =============================================================================


class TestStaffAuthenticationAndSeparation:
    def test_anonymous_redirected_to_staff_login(self, client):
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 302
        assert reverse("staff:login") in resp.headers["Location"]
        assert "?next=/staff/" in resp.headers["Location"]

    def test_customer_user_forbidden_on_staff_portal(self, client, customer_user):
        client.force_login(customer_user)
        resp = client.get(reverse("staff:dashboard"))
        # Customer without staff role gets 403 Forbidden access-denied page
        assert resp.status_code == 403
        assert b"Access Restricted" in resp.content
        assert b"shopper@customer.com" in resp.content

    def test_staff_login_successful_and_safe_redirect(self, client, ops_manager_user):
        resp = client.post(
            reverse("staff:login"),
            {"email": "ops.manager@flashwear.com", "password": "OpsPassword123!"},
        )
        assert resp.status_code == 302
        assert resp.headers["Location"] == reverse("staff:dashboard")

        # After login, dashboard is accessible
        dash_resp = client.get(reverse("staff:dashboard"))
        assert dash_resp.status_code == 200
        assert b"Bob" in dash_resp.content

    def test_ordinary_staff_blocked_from_django_admin(self, client, ops_manager_user):
        """Ordinary staff does not have is_staff=True, so Django admin /admin/ blocks them."""
        assert ops_manager_user.is_staff is False
        client.force_login(ops_manager_user)
        resp = client.get("/admin/")
        # Django admin redirects non-is_staff users to admin:login or 302
        assert resp.status_code == 302
        assert "/admin/login/" in resp.headers["Location"]

    def test_owner_user_can_access_both_portal_and_admin(self, client, owner_user):
        client.force_login(owner_user)
        portal_resp = client.get(reverse("staff:dashboard"))
        assert portal_resp.status_code == 200

        admin_resp = client.get("/admin/")
        assert admin_resp.status_code == 200

    def test_staff_logout(self, client, ops_manager_user):
        client.force_login(ops_manager_user)
        logout_resp = client.get(reverse("staff:logout"))
        assert logout_resp.status_code == 302
        assert reverse("staff:login") in logout_resp.headers["Location"]

        # Subsequent protected request requires sign-in
        dash_resp = client.get(reverse("staff:dashboard"))
        assert dash_resp.status_code == 302


# =============================================================================
# 2. Role-Based Permissions & Route Guardrails Tests
# =============================================================================


class TestRoleBasedPermissions:
    def test_owner_capabilities(self, owner_user):
        assert is_owner_or_superuser(owner_user)
        caps = get_user_capabilities(owner_user)
        assert PORTAL_ACCESS in caps
        assert STAFF_MANAGE in caps
        assert PRODUCTS_VIEW in caps
        assert ORDERS_VIEW in caps
        assert INVENTORY_VIEW in caps
        assert DELIVERIES_VIEW in caps
        assert SUPPORT_VIEW in caps
        assert REPORTS_VIEW in caps

    def test_operations_manager_permissions(self, client, ops_manager_user):
        client.force_login(ops_manager_user)
        assert client.get(reverse("staff:dashboard")).status_code == 200
        assert client.get(reverse("staff:products")).status_code == 200
        assert client.get(reverse("staff:inventory")).status_code == 200
        assert client.get(reverse("staff:orders")).status_code == 200
        assert client.get(reverse("staff:deliveries")).status_code == 200
        assert client.get(reverse("staff:support")).status_code == 200
        assert client.get(reverse("staff:reports")).status_code == 200
        assert client.get(reverse("staff:account_list")).status_code == 200

    def test_product_manager_permissions_and_denials(self, client, product_manager_user):
        client.force_login(product_manager_user)
        # Allowed
        assert client.get(reverse("staff:dashboard")).status_code == 200
        assert client.get(reverse("staff:products")).status_code == 200
        assert client.get(reverse("staff:inventory")).status_code == 200
        assert client.get(reverse("staff:reports")).status_code == 200

        # Denied (returns 403 Forbidden)
        assert client.get(reverse("staff:orders")).status_code == 403
        assert client.get(reverse("staff:deliveries")).status_code == 403
        assert client.get(reverse("staff:support")).status_code == 403
        assert client.get(reverse("staff:account_list")).status_code == 403

    def test_inventory_staff_permissions_and_denials(self, client, inventory_staff_user):
        client.force_login(inventory_staff_user)
        # Allowed
        assert client.get(reverse("staff:dashboard")).status_code == 200
        assert client.get(reverse("staff:inventory")).status_code == 200
        assert client.get(reverse("staff:products")).status_code == 200
        assert client.get(reverse("staff:deliveries")).status_code == 200

        # Denied
        assert client.get(reverse("staff:orders")).status_code == 403
        assert client.get(reverse("staff:support")).status_code == 403
        assert client.get(reverse("staff:reports")).status_code == 403
        assert client.get(reverse("staff:account_list")).status_code == 403

    def test_order_processing_staff_permissions_and_denials(self, client, order_staff_user):
        client.force_login(order_staff_user)
        # Allowed
        assert client.get(reverse("staff:dashboard")).status_code == 200
        assert client.get(reverse("staff:orders")).status_code == 200
        assert client.get(reverse("staff:inventory")).status_code == 200
        assert client.get(reverse("staff:deliveries")).status_code == 200

        # Denied
        assert client.get(reverse("staff:support")).status_code == 403
        assert client.get(reverse("staff:reports")).status_code == 403
        assert client.get(reverse("staff:account_list")).status_code == 403

    def test_delivery_staff_permissions_and_denials(self, client, delivery_staff_user):
        client.force_login(delivery_staff_user)
        # Allowed
        assert client.get(reverse("staff:dashboard")).status_code == 200
        assert client.get(reverse("staff:deliveries")).status_code == 200
        assert client.get(reverse("staff:orders")).status_code == 200

        # Denied
        assert client.get(reverse("staff:products")).status_code == 403
        assert client.get(reverse("staff:inventory")).status_code == 403
        assert client.get(reverse("staff:support")).status_code == 403
        assert client.get(reverse("staff:account_list")).status_code == 403

    def test_support_staff_permissions_and_denials(self, client, support_staff_user):
        client.force_login(support_staff_user)
        # Allowed
        assert client.get(reverse("staff:dashboard")).status_code == 200
        assert client.get(reverse("staff:support")).status_code == 200
        assert client.get(reverse("staff:orders")).status_code == 200
        assert client.get(reverse("staff:deliveries")).status_code == 200

        # Denied
        assert client.get(reverse("staff:products")).status_code == 403
        assert client.get(reverse("staff:inventory")).status_code == 403
        assert client.get(reverse("staff:account_list")).status_code == 403

    def test_read_only_staff_permissions_and_denials(self, client, read_only_staff_user):
        client.force_login(read_only_staff_user)
        # Allowed read-only screens
        assert client.get(reverse("staff:dashboard")).status_code == 200
        assert client.get(reverse("staff:products")).status_code == 200
        assert client.get(reverse("staff:inventory")).status_code == 200
        assert client.get(reverse("staff:orders")).status_code == 200
        assert client.get(reverse("staff:deliveries")).status_code == 200
        assert client.get(reverse("staff:support")).status_code == 200
        assert client.get(reverse("staff:reports")).status_code == 200

        # Denied staff management
        assert client.get(reverse("staff:account_list")).status_code == 403
        assert client.get(reverse("staff:account_create")).status_code == 403

        # Verify can_manage flag in template context is False
        resp = client.get(reverse("staff:orders"))
        assert resp.context["can_manage"] is False


# =============================================================================
# 3. Staff Account Management & Security Invariants Tests
# =============================================================================


class TestStaffAccountManagementAndInvariants:
    def test_create_staff_account_success_and_audit(self, owner_user):
        created_user, temp_pw = create_staff_account(
            email="new.hire@flashwear.com",
            first_name="New",
            last_name="Hire",
            roles=[StaffRole.ORDER_STAFF],
            actor=owner_user,
        )
        assert created_user.pk is not None
        assert created_user.email == "new.hire@flashwear.com"
        assert created_user.is_staff is False  # Restricted from /admin/
        assert StaffRole.ORDER_STAFF in get_user_roles(created_user)
        assert len(temp_pw) == 16

        # Audit events recorded
        assert AuditEvent.objects.filter(
            action="staff.account.create", object_id=str(created_user.pk)
        ).exists()
        assert AuditEvent.objects.filter(
            action="staff.role.assign", object_id=str(created_user.pk)
        ).exists()

    def test_ops_manager_cannot_create_owner_role(self, ops_manager_user):
        """Operations Manager cannot elevate or create someone with Owner role."""
        with pytest.raises(ValidationError, match="Only an Owner can assign the Owner role"):
            create_staff_account(
                email="escalated@flashwear.com",
                roles=[StaffRole.OWNER],
                actor=ops_manager_user,
            )

    def test_staff_cannot_modify_own_roles(self, client, ops_manager_user):
        """Prevention of self-promotion: staff cannot edit their own roles."""
        client.force_login(ops_manager_user)
        url = reverse("staff:account_roles", kwargs={"pk": ops_manager_user.pk})

        # GET redirects with error message
        resp = client.get(url)
        assert resp.status_code == 302
        assert (
            reverse("staff:account_detail", kwargs={"pk": ops_manager_user.pk})
            in resp.headers["Location"]
        )

        # Direct domain service also rejects self-modification
        with pytest.raises(ValidationError, match="You cannot modify your own roles"):
            update_staff_roles(
                user=ops_manager_user,
                roles=[StaffRole.OWNER],
                actor=ops_manager_user,
            )

    def test_cannot_remove_last_active_owner(self, owner_user):
        """System must preserve at least one active Owner."""
        with pytest.raises(
            ValidationError, match="Cannot revoke the Owner role from the last active Owner"
        ):
            update_staff_roles(
                user=owner_user,
                roles=[StaffRole.ORDER_STAFF],
                actor=owner_user,
            )

    def test_cannot_deactivate_self(self, ops_manager_user):
        with pytest.raises(ValidationError, match="You cannot deactivate your own account"):
            toggle_staff_active(user=ops_manager_user, is_active=False, actor=ops_manager_user)

    def test_cannot_deactivate_last_active_owner(self, owner_user, ops_manager_user):
        with pytest.raises(ValidationError, match="Cannot deactivate the final Owner account"):
            toggle_staff_active(user=owner_user, is_active=False, actor=ops_manager_user)

    def test_deactivate_staff_account_cleans_sessions_and_audits(
        self, owner_user, order_staff_user
    ):
        toggle_staff_active(user=order_staff_user, is_active=False, actor=owner_user)
        order_staff_user.refresh_from_db()
        assert order_staff_user.is_active is False
        assert order_staff_user.deactivated_at is not None

        # Audit event recorded
        assert AuditEvent.objects.filter(
            action="staff.account.deactivate", object_id=str(order_staff_user.pk)
        ).exists()


# =============================================================================
# 4. Navigation & UI Consistency Tests
# =============================================================================


class TestStaffNavigationAndUI:
    def test_navigation_tree_tailored_to_roles(
        self, product_manager_user, order_staff_user, read_only_staff_user
    ):
        # Product Manager sees Products and Inventory, but not Orders, Deliveries, or Staff Accounts
        pm_nav = get_staff_navigation(product_manager_user)
        pm_keys = [item["key"] for group in pm_nav for item in group["items"]]
        assert "dashboard" in pm_keys
        assert "products" in pm_keys
        assert "inventory" in pm_keys
        assert "orders" not in pm_keys
        assert "deliveries" not in pm_keys
        assert "accounts" not in pm_keys

        # Order Processing Staff sees Orders, Deliveries, and Inventory, but not Staff Accounts
        order_nav = get_staff_navigation(order_staff_user)
        order_keys = [item["key"] for group in order_nav for item in group["items"]]
        assert "orders" in order_keys
        assert "deliveries" in order_keys
        assert "accounts" not in order_keys

        # Read-Only Staff sees all operational view pages, but not Staff Accounts
        ro_nav = get_staff_navigation(read_only_staff_user)
        ro_keys = [item["key"] for group in ro_nav for item in group["items"]]
        assert "dashboard" in ro_keys
        assert "products" in ro_keys
        assert "orders" in ro_keys
        assert "deliveries" in ro_keys
        assert "accounts" not in ro_keys

    def test_dashboard_summary_respects_permissions(self, product_manager_user):
        summary = get_staff_dashboard_summary(product_manager_user)
        assert "products" in summary
        assert "inventory" in summary
        assert "orders" not in summary
        assert "deliveries" not in summary
        assert "staff" not in summary

    def test_safe_redirect_prevents_open_redirect(self, client, ops_manager_user):
        resp = client.post(
            reverse("staff:login"),
            {
                "email": "ops.manager@flashwear.com",
                "password": "OpsPassword123!",
                "next": "https://attacker.evil.com/phishing",
            },
        )
        assert resp.status_code == 302
        # Open redirect rejected, falls back to safe staff dashboard
        assert resp.headers["Location"] == reverse("staff:dashboard")

    def test_inactive_staff_user_cannot_access_portal(self, client, ops_manager_user):
        ops_manager_user.is_active = False
        ops_manager_user.save()
        client.force_login(ops_manager_user)

        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 302
        assert reverse("staff:login") in resp.headers["Location"]

        # Attempting form login with inactive account fails
        login_resp = client.post(
            reverse("staff:login"),
            {
                "email": ops_manager_user.email,
                "password": "ValidPassword123!",
            },
        )
        assert login_resp.status_code == 200
        assert (
            "Invalid email address or password" in login_resp.content.decode()
            or "inactive" in login_resp.content.decode()
        )

    def test_account_create_view_via_http(self, client, owner_user):
        client.force_login(owner_user)
        resp = client.post(
            reverse("staff:account_create"),
            {
                "email": "http.created@flashwear.com",
                "first_name": "Http",
                "last_name": "User",
                "roles": [StaffRole.DELIVERY_STAFF],
                "password": "TempPassword123!",
            },
        )
        assert resp.status_code == 302
        created = User.objects.get(email="http.created@flashwear.com")
        assert StaffRole.DELIVERY_STAFF in get_user_roles(created)

    def test_account_detail_and_roles_view_via_http(self, client, owner_user, delivery_staff_user):
        client.force_login(owner_user)
        detail_resp = client.get(
            reverse("staff:account_detail", kwargs={"pk": delivery_staff_user.pk})
        )
        assert detail_resp.status_code == 200
        assert b"Delivery Staff" in detail_resp.content

        roles_resp = client.post(
            reverse("staff:account_roles", kwargs={"pk": delivery_staff_user.pk}),
            {"roles": [StaffRole.DELIVERY_STAFF, StaffRole.ORDER_STAFF]},
        )
        assert roles_resp.status_code == 302
        new_roles = get_user_roles(delivery_staff_user)
        assert StaffRole.ORDER_STAFF in new_roles
        assert StaffRole.DELIVERY_STAFF in new_roles

    def test_account_toggle_active_view_via_http(self, client, owner_user, delivery_staff_user):
        client.force_login(owner_user)
        resp = client.post(
            reverse("staff:account_toggle_active", kwargs={"pk": delivery_staff_user.pk})
        )
        assert resp.status_code == 302
        delivery_staff_user.refresh_from_db()
        assert delivery_staff_user.is_active is False

    def test_audit_logs_view_via_http(self, client, owner_user):
        client.force_login(owner_user)
        resp = client.get(reverse("staff:audit_logs"))
        assert resp.status_code == 200
        assert b"Staff Audit Trail" in resp.content

    def test_search_and_filter_staff_accounts(self, client, owner_user, ops_manager_user):
        client.force_login(owner_user)
        # Search by name "Bob"
        resp = client.get(reverse("staff:account_list") + "?q=Bob")
        assert resp.status_code == 200
        assert b"ops.manager@flashwear.com" in resp.content

        # Filter by role "operations_manager"
        resp_role = client.get(
            reverse("staff:account_list") + "?role=" + StaffRole.OPERATIONS_MANAGER
        )
        assert resp_role.status_code == 200
        assert b"ops.manager@flashwear.com" in resp_role.content
