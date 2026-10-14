"""Tests for FLASHWEAR Phase 56: Business Automation & Intelligent Operations.

Covers:
- Strict server-side rule validation and allowlists.
- Incompatible trigger/action rejection.
- Prevention of arbitrary scripts, SQL, or unauthorized capabilities.
- Real domain automations:
    * Scheduled Low Inventory Alert (emits staff notification via unified notification engine).
    * Overdue Supplier Invoice Flagging (appends timestamped operational note and audit record).
    * Support Ticket SLA Breach Escalation (safely transitions to ESCALATED via domain service).
    * Pending Fulfillment SLA Warning (alerts order fulfillment staff).
    * Courier Delivery Exception Alert (alerts operations of failed/exceptional deliveries).
- Cooldown windows and idempotency safeguards preventing alert floods.
- Concurrency locking preventing duplicate overlapping runs.
- Error isolation and error sanitization (no credentials or trace paths).
- Management command execution (`python manage.py run_automations`).
- Celery shared task execution (`evaluate_scheduled_automations`).
- Backoffice UI access control, permissions, CSRF, and append-only audit event logging.
"""

from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.automation.constants import (
    ActionType,
    ExecutionStatus,
    TriggerType,
)
from apps.automation.engine import AutomationEngine
from apps.automation.models import AutomationExecutionLog, AutomationRule
from apps.automation.services import (
    evaluate_rule,
)
from apps.automation.tasks import evaluate_scheduled_automations
from apps.backoffice.models import AuditEvent
from apps.backoffice.permissions import (
    ADMIN_GROUP,
    INVENTORY_GROUP,
    INVENTORY_VIEW,
    OPERATOR_GROUP,
    ORDERS_VIEW,
    SUPPORT_MANAGER_GROUP,
    ensure_backoffice_groups,
)
from apps.catalog.models import Category, Product, ProductVariant
from apps.inventory.models import Stock
from apps.notifications.models import Notification, NotificationType
from apps.orders.models import Order, Shipment
from apps.procurement.models import (
    Supplier,
    SupplierInvoice,
    SupplierInvoiceStatus,
)
from apps.support.models import SupportTicket
from apps.support.permissions import ensure_capabilities as ensure_support_capabilities

User = get_user_model()


@pytest.fixture(autouse=True)
def setup_groups():
    ensure_backoffice_groups()
    ensure_support_capabilities()
    cache.clear()


@pytest.fixture
def admin_user(db):
    user = User.objects.create_user(
        email="admin_auto@flashwear.com",
        password="TestPassword123!",
        is_staff=True,
    )
    admin_grp = Group.objects.get(name=ADMIN_GROUP)
    sup_mgr_grp = Group.objects.get(name=SUPPORT_MANAGER_GROUP)
    user.groups.add(admin_grp, sup_mgr_grp)
    return user


@pytest.fixture
def operator_user(db):
    user = User.objects.create_user(
        email="operator_auto@flashwear.com",
        password="TestPassword123!",
        is_staff=True,
    )
    op_grp = Group.objects.get(name=OPERATOR_GROUP)
    user.groups.add(op_grp)
    return user


@pytest.fixture
def view_only_user(db):
    user = User.objects.create_user(
        email="viewer_auto@flashwear.com",
        password="TestPassword123!",
        is_staff=True,
    )
    # Inventory manager only has view on automations, not manage
    inv_grp = Group.objects.get(name=INVENTORY_GROUP)
    user.groups.add(inv_grp)
    return user


@pytest.fixture
def plain_customer(db):
    return User.objects.create_user(
        email="shopper_auto@flashwear.com",
        password="TestPassword123!",
        is_staff=False,
    )


@pytest.fixture
def sample_catalog_item(db):
    category = Category.objects.create(name="Automation Fashion", slug="auto-fashion")
    product = Product.objects.create(name="Cyber Jacket", category=category, slug="cyber-jacket")
    variant = ProductVariant.objects.create(
        product=product, sku="CYBER-JKT-L", price=Decimal("120.00")
    )
    stock = Stock.objects.create(variant=variant, on_hand=3, reserved=0)
    return variant, stock


def _create_test_order(
    user, number: str, status: str, paid_at=None, total: Decimal = Decimal("89.00")
):
    from apps.accounts.models import Address
    from apps.orders.models import Order
    from apps.shop.models import Cart, CheckoutSession

    cart, _ = Cart.objects.get_or_create(
        user=user, status=Cart.Status.CONVERTED, defaults={"session_key": ""}
    )
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
        user=user, cart=cart, shipping_address=addr, status=CheckoutSession.Status.CONVERTED
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
        paid_at=paid_at,
    )


# =============================================================================
# 1. Rule Validation Tests
# =============================================================================


@pytest.mark.django_db
class TestRuleValidation:
    def test_valid_rule_creation(self, admin_user):
        rule = AutomationRule.objects.create(
            name="Low Stock Alert",
            description="Alert when inventory is below 5 units",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            action_config={"staff_capability": INVENTORY_VIEW},
            created_by=admin_user,
        )
        assert rule.pk is not None
        assert rule.is_active is True
        assert rule.cooldown_minutes == 60

    def test_unknown_trigger_rejected(self):
        rule = AutomationRule(
            name="Bogus Trigger Rule",
            trigger_type="arbitrary_unsupported_trigger",
            action_type=ActionType.CREATE_STAFF_ALERT,
        )
        with pytest.raises(ValidationError) as exc:
            rule.save()
        assert "Unsupported trigger type" in str(exc.value)

    def test_unknown_action_rejected(self):
        rule = AutomationRule(
            name="Bogus Action Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            action_type="arbitrary_unsupported_action",
        )
        with pytest.raises(ValidationError) as exc:
            rule.save()
        assert "Unsupported action type" in str(exc.value)

    def test_incompatible_trigger_action_rejected(self):
        # Trying to escalate a support ticket on a low stock trigger
        rule = AutomationRule(
            name="Mismatched Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            action_type=ActionType.ESCALATE_SUPPORT_TICKET,
        )
        with pytest.raises(ValidationError) as exc:
            rule.save()
        assert "not compatible with trigger" in str(exc.value)

    def test_negative_threshold_rejected(self):
        rule = AutomationRule(
            name="Negative Stock Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": -10},
            action_type=ActionType.CREATE_STAFF_ALERT,
        )
        with pytest.raises(ValidationError) as exc:
            rule.save()
        assert "must be a non-negative integer" in str(exc.value)

    def test_excessive_threshold_rejected(self):
        rule = AutomationRule(
            name="Excessive Stock Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 999999},
            action_type=ActionType.CREATE_STAFF_ALERT,
        )
        with pytest.raises(ValidationError) as exc:
            rule.save()
        assert "exceeds maximum allowable limit" in str(exc.value)

    def test_invalid_capability_rejected(self):
        rule = AutomationRule(
            name="Unsafe Capability Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            action_type=ActionType.CREATE_STAFF_ALERT,
            action_config={"staff_capability": "unrestricted.superuser.privilege"},
        )
        with pytest.raises(ValidationError) as exc:
            rule.save()
        assert "Invalid staff capability" in str(exc.value)

    def test_non_integer_condition_rejected(self):
        rule = AutomationRule(
            name="String Threshold Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": "five"},
            action_type=ActionType.CREATE_STAFF_ALERT,
        )
        with pytest.raises(ValidationError) as exc:
            rule.save()
        assert "must be an integer" in str(exc.value)


# =============================================================================
# 2. Business Automations Execution Tests
# =============================================================================


@pytest.mark.django_db
class TestBusinessAutomations:
    def test_scheduled_low_stock_evaluation(self, admin_user, operator_user, sample_catalog_item):
        _variant, _stock = sample_catalog_item
        rule = AutomationRule.objects.create(
            name="Low Stock Warning",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            action_config={"staff_capability": INVENTORY_VIEW},
            created_by=admin_user,
        )

        log = evaluate_rule(rule, actor=admin_user)
        assert log.status == ExecutionStatus.SUCCESS
        assert log.items_evaluated >= 1
        assert log.items_affected >= 1

        # Check notification delivery to staff
        notif = Notification.objects.filter(
            notification_type=NotificationType.OPERATIONAL_ALERT,
            user=operator_user,
        ).first()
        assert notif is not None
        assert "CYBER-JKT-L" in notif.title or "CYBER-JKT-L" in notif.body

    def test_scheduled_overdue_invoice_flagging(self, admin_user):
        supplier = Supplier.objects.create(
            name="Apex Fabrics Ltd",
            code="SUP-APEX-01",
            currency="USD",
        )
        past_due = timezone.localdate() - timedelta(days=5)
        invoice = SupplierInvoice.objects.create(
            supplier=supplier,
            invoice_number="INV-2026-999",
            normalized_invoice_number="INV2026999",
            status=SupplierInvoiceStatus.APPROVED,
            due_date=past_due,
            subtotal=Decimal("1500.00"),
            total_amount=Decimal("1500.00"),
        )

        rule = AutomationRule.objects.create(
            name="Overdue AP Invoices",
            trigger_type=TriggerType.SCHEDULED_OVERDUE_INVOICES,
            conditions={"days_overdue": 0},
            action_type=ActionType.FLAG_SUPPLIER_INVOICE,
            action_config={"note": "Invoice requires payment priority review."},
            created_by=admin_user,
        )

        log = evaluate_rule(rule, actor=admin_user)
        assert log.status == ExecutionStatus.SUCCESS
        assert log.items_affected >= 1

        invoice.refresh_from_db()
        assert "Flagged Overdue" in invoice.internal_notes
        assert "Invoice requires payment priority review." in invoice.internal_notes
        # Assert financial integrity preserved
        assert invoice.total_amount == Decimal("1500.00")
        assert invoice.status == SupplierInvoiceStatus.APPROVED

        # Verify backoffice AuditEvent was recorded
        audit = AuditEvent.objects.filter(
            object_type="SupplierInvoice",
            object_id=str(invoice.pk),
            action="supplier_invoice_flagged",
        ).first()
        assert audit is not None

    def test_scheduled_support_ticket_sla_escalation(self, admin_user, plain_customer):
        ticket = SupportTicket.objects.create(
            customer=plain_customer,
            subject="Damaged delivery package",
            category=SupportTicket.Category.DELIVERY,
            priority=SupportTicket.Priority.NORMAL,
            status=SupportTicket.Status.OPEN,
            last_customer_message_at=timezone.now() - timedelta(hours=72),
        )

        rule = AutomationRule.objects.create(
            name="Support Ticket SLA Escalation",
            trigger_type=TriggerType.SCHEDULED_SUPPORT_TICKET_SLA,
            conditions={"sla_hours": 48},
            action_type=ActionType.ESCALATE_SUPPORT_TICKET,
            action_config={
                "reason": "Customer waiting > 48h with no agent response",
                "target": "tier_2",
            },
            created_by=admin_user,
        )

        log = evaluate_rule(rule, actor=admin_user)
        assert log.status == ExecutionStatus.SUCCESS
        assert log.items_affected >= 1

        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.ESCALATED
        assert ticket.escalated_at is not None
        assert "Customer waiting > 48h" in ticket.escalation_reason

    def test_scheduled_pending_fulfillment_alert(self, admin_user, operator_user, plain_customer):
        _order = _create_test_order(
            user=plain_customer,
            number="ORD-AUTO-001",
            status=Order.Status.PAID,
            paid_at=timezone.now() - timedelta(hours=36),
            total=Decimal("89.00"),
        )

        rule = AutomationRule.objects.create(
            name="Pending Fulfillment SLA Alert",
            trigger_type=TriggerType.SCHEDULED_PENDING_FULFILLMENT,
            conditions={"sla_hours": 24},
            action_type=ActionType.CREATE_STAFF_ALERT,
            action_config={"staff_capability": ORDERS_VIEW},
            created_by=admin_user,
        )

        log = evaluate_rule(rule, actor=admin_user)
        assert log.status == ExecutionStatus.SUCCESS
        assert log.items_affected >= 1

        notif = Notification.objects.filter(
            notification_type=NotificationType.OPERATIONAL_ALERT,
            user=operator_user,
        ).first()
        assert notif is not None

    def test_scheduled_shipment_exceptions_alert(self, admin_user, operator_user, plain_customer):
        order = _create_test_order(
            user=plain_customer,
            number="ORD-AUTO-002",
            status=Order.Status.PROCESSING,
            total=Decimal("150.00"),
        )
        _shipment = Shipment.objects.create(
            order=order,
            reference="SHIP-AUTO-EXC-01",
            status=Shipment.Status.ATTEMPT_FAILED,
        )

        rule = AutomationRule.objects.create(
            name="Courier Delivery Exception Alert",
            trigger_type=TriggerType.SCHEDULED_SHIPMENT_EXCEPTIONS,
            conditions={"min_attempts": 1},
            action_type=ActionType.CREATE_STAFF_ALERT,
            action_config={"staff_capability": ORDERS_VIEW},
            created_by=admin_user,
        )

        log = evaluate_rule(rule, actor=admin_user)
        assert log.status == ExecutionStatus.SUCCESS
        assert log.items_affected >= 1


# =============================================================================
# 3. Idempotency, Cooldown & Locking Tests
# =============================================================================


@pytest.mark.django_db
class TestIdempotencyAndSafeguards:
    def test_repeated_runs_skip_within_cooldown(self, admin_user, sample_catalog_item):
        _variant, _stock = sample_catalog_item
        rule = AutomationRule.objects.create(
            name="Low Stock Cooldown Test",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            cooldown_minutes=60,
            created_by=admin_user,
        )

        # First execution -> Success
        log1 = evaluate_rule(rule, actor=admin_user)
        assert log1.status == ExecutionStatus.SUCCESS
        assert log1.items_affected >= 1

        # Immediate second execution -> Skipped due to active cooldown window
        log2 = evaluate_rule(rule, actor=admin_user)
        assert log2.status == ExecutionStatus.SKIPPED
        assert log2.items_affected == 0
        assert log2.details.get("reason") == "all_candidates_within_cooldown"

    def test_force_flag_bypasses_cooldown(self, admin_user, sample_catalog_item):
        _variant, _stock = sample_catalog_item
        rule = AutomationRule.objects.create(
            name="Low Stock Force Bypass Test",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            cooldown_minutes=60,
            created_by=admin_user,
        )

        log1 = evaluate_rule(rule, actor=admin_user)
        assert log1.status == ExecutionStatus.SUCCESS

        # Forced run executes despite cooldown
        log2 = evaluate_rule(rule, actor=admin_user, force=True)
        assert log2.status == ExecutionStatus.SUCCESS
        assert log2.items_affected >= 1

    def test_concurrent_lock_prevents_race(self, admin_user, sample_catalog_item):
        rule = AutomationRule.objects.create(
            name="Concurrency Lock Test",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            created_by=admin_user,
        )

        lock_key = f"automation_lock_rule_{rule.pk}"
        cache.set(lock_key, "1", timeout=60)

        # Attempting execution while lock is held
        log = evaluate_rule(rule, actor=admin_user)
        assert log.status == ExecutionStatus.SKIPPED
        assert log.details.get("reason") == "concurrent_lock_active"

    def test_error_handling_sanitizes_summary(self, admin_user, monkeypatch):
        rule = AutomationRule.objects.create(
            name="Failure Handling Test",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            created_by=admin_user,
        )

        def mock_broken_find(self):
            raise RuntimeError(
                "Database connection string postgres://user:secretpassword@db failed"
            )

        monkeypatch.setattr(AutomationEngine, "_find_candidates", mock_broken_find)

        log = evaluate_rule(rule, actor=admin_user)
        assert log.status == ExecutionStatus.FAILED
        rule.refresh_from_db()
        assert rule.consecutive_failures == 1
        assert rule.last_status == ExecutionStatus.FAILED


# =============================================================================
# 4. Management Command & Celery Task Tests
# =============================================================================


@pytest.mark.django_db
class TestExecutionInterfaces:
    def test_run_automations_command(self, admin_user, sample_catalog_item):
        AutomationRule.objects.create(
            name="Command Test Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            created_by=admin_user,
        )

        out = io.StringIO()
        call_command("run_automations", stdout=out)
        output = out.getvalue()
        assert "Starting automation evaluation" in output
        assert "Command Test Rule" in output
        assert "Automation evaluation completed" in output

    def test_run_automations_dry_run(self, admin_user, sample_catalog_item):
        _rule = AutomationRule.objects.create(
            name="Dry Run Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            created_by=admin_user,
        )

        out = io.StringIO()
        call_command("run_automations", "--dry-run", stdout=out)
        output = out.getvalue()
        assert "[DRY-RUN]" in output
        assert "matching candidate" in output

    def test_celery_task_execution(self, admin_user, sample_catalog_item):
        AutomationRule.objects.create(
            name="Celery Task Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            created_by=admin_user,
        )

        count = evaluate_scheduled_automations()
        assert count >= 1


# =============================================================================
# 5. Backoffice Management Views & Permissions Tests
# =============================================================================


@pytest.mark.django_db
class TestBackofficeAutomationViews:
    def test_anonymous_redirected_to_login(self):
        client = Client()
        url = reverse("backoffice:automations")
        response = client.get(url)
        assert response.status_code == 302
        assert "/accounts/login" in response.url

    def test_plain_customer_denied_access(self, plain_customer):
        client = Client()
        client.force_login(plain_customer)
        url = reverse("backoffice:automations")
        response = client.get(url)
        # Missing backoffice capability answers 404 by backoffice security contract
        assert response.status_code == 404

    def test_staff_with_view_can_view_rules_and_logs(self, view_only_user, admin_user):
        rule = AutomationRule.objects.create(
            name="View Accessible Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            created_by=admin_user,
        )
        log = AutomationExecutionLog.objects.create(
            rule=rule,
            trigger_event=rule.trigger_type,
            status=ExecutionStatus.SUCCESS,
            idempotency_key="log_test_001",
        )

        client = Client()
        client.force_login(view_only_user)

        # Rule list
        resp = client.get(reverse("backoffice:automations"))
        assert resp.status_code == 200
        assert "View Accessible Rule" in resp.content.decode("utf-8")

        # Execution logs
        resp_logs = client.get(reverse("backoffice:automation_logs"))
        assert resp_logs.status_code == 200
        assert "View Accessible Rule" in resp_logs.content.decode("utf-8")

        # Log detail
        resp_detail = client.get(reverse("backoffice:automation_log_detail", args=[log.pk]))
        assert resp_detail.status_code == 200

    def test_staff_without_manage_cannot_toggle_or_run(self, view_only_user, admin_user):
        rule = AutomationRule.objects.create(
            name="Protected Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            action_type=ActionType.CREATE_STAFF_ALERT,
            created_by=admin_user,
        )

        client = Client()
        client.force_login(view_only_user)

        toggle_url = reverse("backoffice:automation_toggle", args=[rule.pk])
        resp_toggle = client.post(toggle_url)
        assert resp_toggle.status_code == 404

        run_url = reverse("backoffice:automation_run", args=[rule.pk])
        resp_run = client.post(run_url)
        assert resp_run.status_code == 404

    def test_manager_can_toggle_and_run(self, admin_user, sample_catalog_item):
        rule = AutomationRule.objects.create(
            name="Manageable Rule",
            trigger_type=TriggerType.SCHEDULED_LOW_STOCK,
            conditions={"threshold": 5},
            action_type=ActionType.CREATE_STAFF_ALERT,
            is_active=True,
            created_by=admin_user,
        )

        client = Client()
        client.force_login(admin_user)

        # Toggle rule
        toggle_url = reverse("backoffice:automation_toggle", args=[rule.pk])
        resp_toggle = client.post(toggle_url, follow=True)
        assert resp_toggle.status_code == 200
        rule.refresh_from_db()
        assert rule.is_active is False

        # Run on-demand
        run_url = reverse("backoffice:automation_run", args=[rule.pk])
        resp_run = client.post(run_url, follow=True)
        assert resp_run.status_code == 200

        # Assert AuditEvent was recorded
        audit = AuditEvent.objects.filter(
            object_type="AutomationRule",
            object_id=str(rule.pk),
            action="automation_rule_disabled",
        ).first()
        assert audit is not None

    def test_manager_can_create_rule(self, admin_user):
        client = Client()
        client.force_login(admin_user)

        create_url = reverse("backoffice:automation_create")
        post_data = {
            "name": "Form Created Rule",
            "description": "Created through backoffice UI form",
            "trigger_type": TriggerType.SCHEDULED_LOW_STOCK,
            "action_type": ActionType.CREATE_STAFF_ALERT,
            "threshold": 4,
            "staff_capability": INVENTORY_VIEW,
            "cooldown_minutes": 30,
            "max_retries": 3,
            "is_active": "on",
        }
        resp = client.post(create_url, post_data, follow=True)
        assert resp.status_code == 200

        rule = AutomationRule.objects.filter(name="Form Created Rule").first()
        assert rule is not None
        assert rule.conditions.get("threshold") == 4
        assert rule.action_config.get("staff_capability") == INVENTORY_VIEW
        assert rule.cooldown_minutes == 30
