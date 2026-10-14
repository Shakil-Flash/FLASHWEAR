"""Comprehensive test suite for FLASHWEAR Phase 63: Staff Tasks, Notifications & Collaboration."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import Notification, NotificationCategory, NotificationType
from apps.orders.models import Order
from apps.shop.models import Cart, CheckoutSession
from apps.staff.models import (
    StaffTask,
    StaffTaskPriority,
    StaffTaskRelatedType,
    StaffTaskStatus,
)
from apps.staff.notification_services import (
    get_staff_unread_count,
    mark_all_staff_notifications_read,
    mark_staff_notification_read,
    notify_staff_user,
)
from apps.staff.permissions import ensure_staff_groups
from apps.staff.task_services import (
    add_task_comment,
    can_user_assign_task,
    can_user_comment,
    can_user_update_progress,
    can_user_update_status,
    can_user_view_task,
    create_staff_task,
    get_linked_object_staff_url,
    reassign_staff_task,
    sweep_approaching_and_overdue_tasks,
    update_task_progress,
    update_task_status,
)

User = get_user_model()


@pytest.fixture(autouse=True)
def setup_groups():
    ensure_staff_groups()


@pytest.fixture
def owner_user(db):
    user = User.objects.create_user(
        email="owner@flashwear.com",
        password="Password123!",
        first_name="Admin",
        last_name="Owner",
        is_staff=True,
    )
    group = Group.objects.get(name="Owner")
    user.groups.add(group)
    return user


@pytest.fixture
def ops_manager(db):
    user = User.objects.create_user(
        email="ops@flashwear.com",
        password="Password123!",
        first_name="Ops",
        last_name="Manager",
        is_staff=True,
    )
    group = Group.objects.get(name="Operations Manager")
    user.groups.add(group)
    return user


@pytest.fixture
def delivery_staff(db):
    user = User.objects.create_user(
        email="driver@flashwear.com",
        password="Password123!",
        first_name="Dave",
        last_name="Driver",
        is_staff=True,
    )
    group = Group.objects.get(name="Delivery Staff")
    user.groups.add(group)
    return user


@pytest.fixture
def read_only_staff(db):
    user = User.objects.create_user(
        email="auditor@flashwear.com",
        password="Password123!",
        first_name="Auditor",
        last_name="Reader",
        is_staff=True,
    )
    group = Group.objects.get(name="Read-Only Staff")
    user.groups.add(group)
    return user


@pytest.fixture
def customer_user(db):
    return User.objects.create_user(
        email="shopper@example.com",
        password="Password123!",
        first_name="Jane",
        last_name="Customer",
    )


# --------------------------------------------------------------------------------------
# 1. Staff Task Lifecycle & Workflow Tests
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_task_by_manager(ops_manager, delivery_staff):
    """Authorized manager creates task, assigns to staff member, and triggers notification."""
    due = timezone.now() + timedelta(days=2)
    task = create_staff_task(
        creator=ops_manager,
        title="Check damaged parcel",
        description="Verify parcel contents at sorting hub",
        priority=StaffTaskPriority.HIGH,
        assignee=delivery_staff,
        due_date=due,
    )

    assert task.pk is not None
    assert task.status == StaffTaskStatus.OPEN
    assert task.priority == StaffTaskPriority.HIGH
    assert task.creator == ops_manager
    assert task.assignee == delivery_staff
    assert task.progress_percent == 0

    # Activity trail
    activities = list(task.activities.all())
    assert len(activities) >= 2
    actions = [a.action for a in activities]
    assert "created" in actions
    assert "assigned" in actions

    # In-app notification for assignee
    notif = Notification.objects.filter(
        user=delivery_staff,
        notification_type=NotificationType.STAFF_TASK_ASSIGNED,
    ).first()
    assert notif is not None
    assert str(task.pk) in notif.title
    assert notif.category == NotificationCategory.STAFF
    assert notif.action_url == f"/staff/tasks/{task.pk}/"


@pytest.mark.django_db
def test_create_task_unauthorized(delivery_staff):
    """Staff without TASKS_MANAGE cannot create team tasks."""
    assert not can_user_assign_task(delivery_staff)
    with pytest.raises(PermissionDenied):
        create_staff_task(
            creator=delivery_staff,
            title="Unauthorized task",
        )


@pytest.mark.django_db
def test_create_task_invalid_assignee(ops_manager, customer_user):
    """Tasks cannot be assigned to customer or deactivated users."""
    with pytest.raises(ValidationError):
        create_staff_task(
            creator=ops_manager,
            title="Task for customer",
            assignee=customer_user,
        )


@pytest.mark.django_db
def test_task_status_transitions(ops_manager, delivery_staff):
    """Enforces valid lifecycle transitions and terminal rules."""
    task = create_staff_task(
        creator=ops_manager,
        title="Fulfill expedited order",
        assignee=delivery_staff,
    )

    # Valid: open -> in_progress
    update_task_status(task=task, new_status=StaffTaskStatus.IN_PROGRESS, actor=delivery_staff)
    assert task.status == StaffTaskStatus.IN_PROGRESS

    # Valid: in_progress -> blocked
    update_task_status(task=task, new_status=StaffTaskStatus.BLOCKED, actor=delivery_staff)
    assert task.status == StaffTaskStatus.BLOCKED

    # Valid: blocked -> completed
    update_task_status(task=task, new_status=StaffTaskStatus.COMPLETED, actor=delivery_staff)
    assert task.status == StaffTaskStatus.COMPLETED
    assert task.completed_at is not None
    assert task.completed_by == delivery_staff
    assert task.progress_percent == 100

    # Ordinary staff cannot reopen a completed task
    assert not can_user_update_status(delivery_staff, task, StaffTaskStatus.OPEN)

    # Manager can reopen
    assert can_user_update_status(ops_manager, task, StaffTaskStatus.OPEN)
    update_task_status(task=task, new_status=StaffTaskStatus.OPEN, actor=ops_manager)
    assert task.status == StaffTaskStatus.OPEN
    assert task.completed_at is None


@pytest.mark.django_db
def test_read_only_staff_cannot_mutate_task(ops_manager, read_only_staff):
    """Read-only staff can view tasks but cannot update status, progress, or comment."""
    task = create_staff_task(
        creator=ops_manager,
        title="Audit task",
    )

    assert can_user_view_task(read_only_staff, task)
    assert not can_user_update_status(read_only_staff, task, StaffTaskStatus.IN_PROGRESS)
    assert not can_user_update_progress(read_only_staff, task)
    assert not can_user_comment(read_only_staff, task)

    with pytest.raises(PermissionDenied):
        update_task_status(task=task, new_status=StaffTaskStatus.IN_PROGRESS, actor=read_only_staff)

    with pytest.raises(PermissionDenied):
        update_task_progress(task=task, progress_percent=50, actor=read_only_staff)

    with pytest.raises(PermissionDenied):
        add_task_comment(task=task, author=read_only_staff, comment_text="Testing comment")


@pytest.mark.django_db
def test_task_reassignment(ops_manager, delivery_staff, owner_user):
    """Manager reassigns task to another staff member and dispatches notification."""
    task = create_staff_task(
        creator=ops_manager,
        title="Transfer stock",
        assignee=delivery_staff,
    )

    reassign_staff_task(task=task, new_assignee=owner_user, actor=ops_manager)
    assert task.assignee == owner_user

    # Activity recorded
    reassigned_act = task.activities.filter(action="reassigned").first()
    assert reassigned_act is not None
    assert delivery_staff.email in reassigned_act.old_value
    assert owner_user.email in reassigned_act.new_value

    # Notification dispatched to new assignee
    notif = Notification.objects.filter(
        user=owner_user,
        notification_type=NotificationType.STAFF_TASK_REASSIGNED,
    ).first()
    assert notif is not None


@pytest.mark.django_db
def test_task_progress_update(ops_manager, delivery_staff):
    """Assignee updates task progress percentage."""
    task = create_staff_task(
        creator=ops_manager,
        title="Inspect pallets",
        assignee=delivery_staff,
    )

    update_task_progress(task=task, progress_percent=40, actor=delivery_staff)
    assert task.progress_percent == 40
    assert task.status == StaffTaskStatus.IN_PROGRESS  # Auto-advanced to in_progress

    # Setting to 100% completes task
    update_task_progress(task=task, progress_percent=100, actor=delivery_staff)
    assert task.status == StaffTaskStatus.COMPLETED
    assert task.completed_at is not None


@pytest.mark.django_db
def test_task_comments(ops_manager, delivery_staff):
    """Staff adds operational comment, records activity, and notifies creator."""
    task = create_staff_task(
        creator=ops_manager,
        title="Check courier SLA",
        assignee=delivery_staff,
    )

    comment = add_task_comment(
        task=task,
        author=delivery_staff,
        comment_text="Package arrived at depot; waiting for inspection report.",
    )
    assert comment.pk is not None
    assert comment.author == delivery_staff

    # Notification dispatched to creator (ops_manager)
    notif = Notification.objects.filter(
        user=ops_manager,
        notification_type=NotificationType.STAFF_TASK_COMMENT,
    ).first()
    assert notif is not None
    assert "depot" in notif.body


# --------------------------------------------------------------------------------------
# 2. Business Object Linking Tests
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_business_object_linking_order(ops_manager, customer_user, delivery_staff):
    """Task linked to Order resolves label and validates permission on URL generation."""
    cart = Cart.objects.create(user=customer_user, session_key="", status=Cart.Status.CONVERTED)
    checkout = CheckoutSession.objects.create(
        user=customer_user, cart=cart, status=CheckoutSession.Status.CONVERTED
    )
    order = Order.objects.create(
        user=customer_user,
        number="FW-99001",
        checkout=checkout,
        subtotal=Decimal("150.00"),
        shipping_amount=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("150.00"),
        status=Order.Status.PROCESSING,
    )

    task = create_staff_task(
        creator=ops_manager,
        title="Investigate order discrepancy",
        related_object_type=StaffTaskRelatedType.ORDER,
        related_object_id=order.pk,
    )

    assert "FW-99001" in task.related_object_label

    # Ops manager has ORDERS_VIEW -> receives link
    ops_url = get_linked_object_staff_url(task, ops_manager)
    assert ops_url == f"/staff/orders/{order.pk}/"

    # Product manager without ORDERS_VIEW -> link is None (preventing IDOR)
    pm = User.objects.create_user(email="pm@flashwear.com", password="Password123!", is_staff=True)
    pm.groups.add(Group.objects.get(name="Product Manager"))
    assert get_linked_object_staff_url(task, pm) is None

    # Customer without portal access or ORDERS_VIEW -> link is None
    assert get_linked_object_staff_url(task, customer_user) is None


# --------------------------------------------------------------------------------------
# 3. Notification Center & Idempotency Tests
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_notification_inbox_and_mark_read(ops_manager, delivery_staff):
    """Staff notifications are user-isolated and mark-read actions work safely."""
    notif1 = notify_staff_user(
        user=delivery_staff,
        notification_type=NotificationType.STAFF_TASK_ASSIGNED,
        title="Task 1 Assigned",
        body="Details 1",
        action_url="/staff/tasks/1/",
    )
    notify_staff_user(
        user=delivery_staff,
        notification_type=NotificationType.STAFF_TASK_STATUS,
        title="Task 2 Status",
        body="Details 2",
        action_url="/staff/tasks/2/",
    )

    assert get_staff_unread_count(delivery_staff) == 2
    # Ops manager sees 0 unread
    assert get_staff_unread_count(ops_manager) == 0

    # User isolation: ops_manager cannot mark delivery_staff's notification read
    res = mark_staff_notification_read(ops_manager, notif1.pk)
    assert res is None
    assert get_staff_unread_count(delivery_staff) == 2

    # Assignee marks notif1 read
    res = mark_staff_notification_read(delivery_staff, notif1.pk)
    assert res is not None
    assert get_staff_unread_count(delivery_staff) == 1

    # Mark all read
    count = mark_all_staff_notifications_read(delivery_staff)
    assert count == 1
    assert get_staff_unread_count(delivery_staff) == 0


@pytest.mark.django_db
def test_notification_idempotency(delivery_staff):
    """Duplicate notification events with same idempotency_key are prevented."""
    key = "staff_task:100:assigned:99"
    notif1 = notify_staff_user(
        user=delivery_staff,
        notification_type=NotificationType.STAFF_TASK_ASSIGNED,
        title="Assigned",
        body="Body 1",
        idempotency_key=key,
    )
    assert notif1 is not None

    notif2 = notify_staff_user(
        user=delivery_staff,
        notification_type=NotificationType.STAFF_TASK_ASSIGNED,
        title="Assigned retry",
        body="Body 2",
        idempotency_key=key,
    )
    assert notif1.pk == notif2.pk
    assert Notification.objects.filter(idempotency_key=key).count() == 1


# --------------------------------------------------------------------------------------
# 4. Deadline & Exception Sweeper Tests
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_deadline_sweeper(ops_manager, delivery_staff):
    """Sweeper detects overdue and approaching tasks and notifies assignees idempotently."""
    now = timezone.now()
    # Overdue task
    create_staff_task(
        creator=ops_manager,
        title="Overdue task item",
        assignee=delivery_staff,
        due_date=now - timedelta(hours=5),
    )
    # Due soon task
    create_staff_task(
        creator=ops_manager,
        title="Due soon item",
        assignee=delivery_staff,
        due_date=now + timedelta(hours=10),
    )
    # Future task (not due soon)
    create_staff_task(
        creator=ops_manager,
        title="Future item",
        assignee=delivery_staff,
        due_date=now + timedelta(days=5),
    )

    sweep_results = sweep_approaching_and_overdue_tasks()
    assert sweep_results["overdue_notified"] >= 1
    assert sweep_results["due_soon_notified"] >= 1

    # Overdue notification received
    overdue_notif = Notification.objects.filter(
        user=delivery_staff,
        notification_type=NotificationType.STAFF_TASK_DEADLINE,
        metadata__kind="overdue",
    ).first()
    assert overdue_notif is not None

    # Due soon notification received
    due_soon_notif = Notification.objects.filter(
        user=delivery_staff,
        notification_type=NotificationType.STAFF_TASK_DEADLINE,
        metadata__kind="due_soon",
    ).first()
    assert due_soon_notif is not None

    # Running sweeper again does not duplicate notifications (idempotent)
    sweep_results_2 = sweep_approaching_and_overdue_tasks()
    assert sweep_results_2["overdue_notified"] == 0
    assert sweep_results_2["due_soon_notified"] == 0


# --------------------------------------------------------------------------------------
# 5. UI Views & Integration Tests
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_task_list_view_authenticated(client, ops_manager, delivery_staff):
    """Task list view renders correctly with metrics, scope filters, and tasks."""
    client.force_login(ops_manager)

    create_staff_task(
        creator=ops_manager,
        title="Restock sneakers",
        assignee=delivery_staff,
    )

    url = reverse("staff:task_list")
    resp = client.get(url)
    assert resp.status_code == 200
    assert "Restock sneakers" in resp.content.decode()
    assert "Staff Tasks" in resp.content.decode()


@pytest.mark.django_db
def test_task_create_view_post(client, ops_manager, delivery_staff):
    """POST to task_create creates task and redirects to task detail workspace."""
    client.force_login(ops_manager)

    url = reverse("staff:task_create")
    data = {
        "title": "Clean sorting conveyor",
        "description": "Routine weekly cleanup",
        "priority": StaffTaskPriority.NORMAL,
        "assignee": delivery_staff.pk,
    }
    resp = client.post(url, data=data)
    assert resp.status_code == 302

    task = StaffTask.objects.filter(title="Clean sorting conveyor").first()
    assert task is not None
    assert task.assignee == delivery_staff
    assert resp.url == reverse("staff:task_detail", kwargs={"pk": task.pk})


@pytest.mark.django_db
def test_task_detail_view_permissions(client, ops_manager, delivery_staff, customer_user):
    """Task detail view restricts unauthorized customer users and shows workspace to staff."""
    task = create_staff_task(
        creator=ops_manager,
        title="Check packing slips",
        assignee=delivery_staff,
    )

    # Customer user denied
    client.force_login(customer_user)
    resp = client.get(reverse("staff:task_detail", kwargs={"pk": task.pk}))
    assert resp.status_code == 403

    # Assigned staff allowed
    client.force_login(delivery_staff)
    resp = client.get(reverse("staff:task_detail", kwargs={"pk": task.pk}))
    assert resp.status_code == 200
    assert "Check packing slips" in resp.content.decode()


@pytest.mark.django_db
def test_notification_center_views(client, delivery_staff):
    """Notification list, mark read, and mark all read views."""
    client.force_login(delivery_staff)

    notif = notify_staff_user(
        user=delivery_staff,
        notification_type=NotificationType.STAFF_TASK_ASSIGNED,
        title="Urgent Task Assigned",
        body="Please look into warehouse aisle 4",
        action_url="/staff/tasks/1/",
    )

    # View list
    resp = client.get(reverse("staff:notification_list"))
    assert resp.status_code == 200
    assert "Urgent Task Assigned" in resp.content.decode()

    # Mark read POST
    resp = client.post(reverse("staff:notification_read_action", kwargs={"pk": notif.pk}))
    assert resp.status_code == 302
    notif.refresh_from_db()
    assert notif.read_at is not None

    # Mark all read POST
    resp = client.post(reverse("staff:notification_mark_all_read_action"))
    assert resp.status_code == 302


@pytest.mark.django_db
def test_staff_dashboard_shows_tasks_metrics(client, ops_manager, delivery_staff):
    """Staff dashboard includes task metrics and urgent alerts."""
    client.force_login(ops_manager)

    create_staff_task(
        creator=ops_manager,
        title="Urgent delivery audit",
        assignee=ops_manager,
        priority=StaffTaskPriority.URGENT,
    )

    resp = client.get(reverse("staff:dashboard"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Staff Tasks" in content
    assert "my open tasks" in content
