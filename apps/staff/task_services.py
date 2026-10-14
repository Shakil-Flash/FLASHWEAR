"""Staff Task Management services, lifecycle enforcement, and business object linking (Phase 63)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.staff.constants import (
    DELIVERIES_VIEW,
    INVENTORY_VIEW,
    ORDERS_VIEW,
    PORTAL_ACCESS,
    SUPPORT_VIEW,
    TASKS_MANAGE,
    TASKS_VIEW,
    StaffRole,
)
from apps.staff.models import (
    StaffTask,
    StaffTaskActivity,
    StaffTaskComment,
    StaffTaskPriority,
    StaffTaskRelatedType,
    StaffTaskStatus,
)
from apps.staff.notification_services import (
    notify_task_assigned,
    notify_task_comment,
    notify_task_deadline_reminder,
    notify_task_reassigned,
    notify_task_status_changed,
)
from apps.staff.permissions import get_user_roles, has_staff_capability

logger = logging.getLogger(__name__)
User = get_user_model()


# --------------------------------------------------------------------------------------
# Assignee Eligibility & Permissions
# --------------------------------------------------------------------------------------


def get_eligible_assignees() -> QuerySet[User]:
    """Return all active users who possess staff portal access."""
    # Active users in any staff group or superusers
    return (
        User.objects.filter(is_active=True)
        .filter(Q(is_superuser=True) | Q(groups__name__in=[role.label for role in StaffRole]))
        .distinct()
        .order_by("first_name", "last_name", "email")
    )


def is_eligible_assignee(user: Any) -> bool:
    """Check if a specific user can be assigned staff tasks."""
    if not user or not getattr(user, "is_active", False):
        return False
    return has_staff_capability(user, PORTAL_ACCESS)


def can_user_view_task(user: Any, task: StaffTask) -> bool:
    """Check if the user is authorized to view this specific task."""
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False

    if not has_staff_capability(user, TASKS_VIEW):
        return False

    # Superuser or manager with TASKS_MANAGE has global visibility
    if user.is_superuser or has_staff_capability(user, TASKS_MANAGE):
        return True

    # Creator or assignee can always view
    if task.creator_id == user.pk or task.assignee_id == user.pk:
        return True

    # Staff with TASKS_VIEW can also view unassigned tasks or team tasks
    return True


def can_user_edit_task(user: Any, task: StaffTask) -> bool:
    """Check if user can modify task title, description, priority, or due date."""
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False

    if user.is_superuser or has_staff_capability(user, TASKS_MANAGE):
        return True

    if task.creator_id == user.pk and has_staff_capability(user, TASKS_MANAGE):
        return True

    return False


def can_user_assign_task(user: Any) -> bool:
    """Check if user has permission to create, assign, or reassign tasks."""
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    return user.is_superuser or has_staff_capability(user, TASKS_MANAGE)


def can_user_update_status(user: Any, task: StaffTask, target_status: str) -> bool:
    """Check if user can transition this task to target_status."""
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False

    # Read-only staff cannot mutate anything
    roles = get_user_roles(user)
    if StaffRole.READ_ONLY in roles and not user.is_superuser:
        return False

    if not task.can_transition_to(target_status):
        return False

    # Reopening completed or cancelled tasks requires TASKS_MANAGE or being the creator
    if task.is_terminal and target_status == StaffTaskStatus.OPEN:
        return (
            user.is_superuser
            or has_staff_capability(user, TASKS_MANAGE)
            or task.creator_id == user.pk
        )

    # Cancelling tasks requires TASKS_MANAGE or creator
    if target_status == StaffTaskStatus.CANCELLED:
        return (
            user.is_superuser
            or has_staff_capability(user, TASKS_MANAGE)
            or task.creator_id == user.pk
        )

    # Normal progress transitions (in_progress, blocked, completed)
    # Permitted for assignee, managers, or creator
    if user.is_superuser or has_staff_capability(user, TASKS_MANAGE):
        return True
    if task.assignee_id == user.pk or task.creator_id == user.pk:
        return True

    return False


def can_user_update_progress(user: Any, task: StaffTask) -> bool:
    """Check if user can update the progress percentage."""
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False

    roles = get_user_roles(user)
    if StaffRole.READ_ONLY in roles and not user.is_superuser:
        return False

    if user.is_superuser or has_staff_capability(user, TASKS_MANAGE):
        return True

    return task.assignee_id == user.pk or task.creator_id == user.pk


def can_user_comment(user: Any, task: StaffTask) -> bool:
    """Check if user can post a comment on this task."""
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False

    roles = get_user_roles(user)
    if StaffRole.READ_ONLY in roles and not user.is_superuser:
        return False

    return can_user_view_task(user, task)


# --------------------------------------------------------------------------------------
# Business Object Linking & Verification
# --------------------------------------------------------------------------------------


def get_linked_object_staff_url(task: StaffTask, user: Any) -> str | None:
    """Validate user authorization to access the linked business object independently.

    Returns the staff URL if authorized, or None if unauthorized or not found.
    Prevents leaking links or unauthorized object access.
    """
    if not task.related_object_type or not task.related_object_id:
        return None

    obj_type = task.related_object_type
    obj_id = task.related_object_id

    if obj_type == StaffTaskRelatedType.ORDER:
        if not (user.is_superuser or has_staff_capability(user, ORDERS_VIEW)):
            return None
        return f"/staff/orders/{obj_id}/"

    elif obj_type == StaffTaskRelatedType.SHIPMENT:
        if not (user.is_superuser or has_staff_capability(user, DELIVERIES_VIEW)):
            return None
        return f"/staff/deliveries/{obj_id}/"

    elif obj_type == StaffTaskRelatedType.VARIANT:
        if not (user.is_superuser or has_staff_capability(user, INVENTORY_VIEW)):
            return None
        return f"/staff/inventory/{obj_id}/"

    elif obj_type == StaffTaskRelatedType.SUPPORT_TICKET:
        if not (user.is_superuser or has_staff_capability(user, SUPPORT_VIEW)):
            return None
        return f"/staff/support/?ticket={obj_id}"

    return None


def resolve_business_object_label(related_type: str, related_id: int) -> str:
    """Generate a clean, non-PII operational label for the linked record."""
    if not related_type or not related_id:
        return ""

    try:
        if related_type == StaffTaskRelatedType.ORDER:
            from apps.orders.models import Order

            order = Order.objects.filter(pk=related_id).first()
            if order:
                num = getattr(order, "number", getattr(order, "order_number", order.pk))
                return f"Order #{num}"

        elif related_type == StaffTaskRelatedType.SHIPMENT:
            from apps.orders.models import Shipment

            shipment = Shipment.objects.filter(pk=related_id).first()
            if shipment:
                tracking = f" ({shipment.tracking_number})" if shipment.tracking_number else ""
                return f"Shipment #{shipment.pk}{tracking}"

        elif related_type == StaffTaskRelatedType.VARIANT:
            from apps.catalog.models import ProductVariant

            variant = ProductVariant.objects.select_related("product").filter(pk=related_id).first()
            if variant:
                return f"Variant: {variant.sku} - {variant.product.name}"

        elif related_type == StaffTaskRelatedType.SUPPORT_TICKET:
            from apps.support.models import SupportTicket

            ticket = SupportTicket.objects.filter(pk=related_id).first()
            if ticket:
                return f"Ticket #{ticket.pk}: {ticket.subject[:40]}"
    except Exception as exc:
        logger.debug("Failed resolving business object label: %s", exc)

    return f"{related_type.capitalize()} #{related_id}"


# --------------------------------------------------------------------------------------
# Task Lifecycle & Workflow Mutations
# --------------------------------------------------------------------------------------


def create_staff_task(
    *,
    creator: Any,
    title: str,
    description: str = "",
    priority: str = StaffTaskPriority.NORMAL,
    assignee: Any = None,
    due_date: Any = None,
    related_object_type: str = StaffTaskRelatedType.NONE,
    related_object_id: int | None = None,
) -> StaffTask:
    """Create a new staff task, validate permissions and assignee, and dispatch notifications."""
    if not can_user_assign_task(creator):
        raise PermissionDenied("You do not have permission to create staff tasks.")

    if not title or not title.strip():
        raise ValidationError({"title": "Task title is required."})

    if assignee is not None and not is_eligible_assignee(assignee):
        raise ValidationError({"assignee": "Selected user is not an eligible active staff member."})

    label = ""
    if related_object_type and related_object_id:
        label = resolve_business_object_label(related_object_type, related_object_id)

    with transaction.atomic():
        task = StaffTask.objects.create(
            title=title.strip(),
            description=description.strip(),
            priority=priority,
            creator=creator,
            assignee=assignee,
            due_date=due_date,
            status=StaffTaskStatus.OPEN,
            progress_percent=0,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            related_object_label=label,
        )

        StaffTaskActivity.objects.create(
            task=task,
            actor=creator,
            action="created",
            new_value=task.title,
            details=f"Task created with {task.get_priority_display()} priority.",
        )

        if assignee:
            StaffTaskActivity.objects.create(
                task=task,
                actor=creator,
                action="assigned",
                new_value=assignee.email,
                details=f"Initially assigned to {assignee.email}.",
            )
            notify_task_assigned(task=task, assigned_by=creator)

    return task


def update_task_fields(
    *,
    task: StaffTask,
    actor: Any,
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    due_date: Any = None,
    due_date_cleared: bool = False,
) -> StaffTask:
    """Update general fields on a task, with activity tracking."""
    if not can_user_edit_task(actor, task):
        raise PermissionDenied("You do not have permission to edit this task.")

    changes = []
    with transaction.atomic():
        if title is not None and title.strip() and title.strip() != task.title:
            old_title = task.title
            task.title = title.strip()
            changes.append(("title", old_title, task.title))

        if description is not None and description.strip() != task.description:
            task.description = description.strip()
            changes.append(("description", "(updated)", "(updated)"))

        if priority is not None and priority != task.priority:
            old_prio = task.priority
            task.priority = priority
            changes.append(("priority", old_prio, task.priority))

        if due_date_cleared:
            if task.due_date is not None:
                changes.append(("due_date", str(task.due_date), "None"))
                task.due_date = None
        elif due_date is not None and due_date != task.due_date:
            old_due = str(task.due_date) if task.due_date else "None"
            task.due_date = due_date
            changes.append(("due_date", old_due, str(task.due_date)))

        task.save()

        for field, old_val, new_val in changes:
            StaffTaskActivity.objects.create(
                task=task,
                actor=actor,
                action=f"{field}_updated",
                old_value=str(old_val)[:255],
                new_value=str(new_val)[:255],
            )

    return task


def reassign_staff_task(
    *,
    task: StaffTask,
    new_assignee: Any,
    actor: Any,
) -> StaffTask:
    """Reassign or unassign a staff task."""
    if not can_user_assign_task(actor):
        raise PermissionDenied("You do not have permission to assign or reassign tasks.")

    if new_assignee is not None and not is_eligible_assignee(new_assignee):
        raise ValidationError({"assignee": "Selected user is not an eligible active staff member."})

    previous_assignee = task.assignee
    if previous_assignee == new_assignee:
        return task

    with transaction.atomic():
        task.assignee = new_assignee
        task.save(update_fields=["assignee", "updated_at"])

        old_label = previous_assignee.email if previous_assignee else "unassigned"
        new_label = new_assignee.email if new_assignee else "unassigned"

        StaffTaskActivity.objects.create(
            task=task,
            actor=actor,
            action="reassigned",
            old_value=old_label,
            new_value=new_label,
            details=f"Task reassigned from {old_label} to {new_label}.",
        )

        if new_assignee:
            notify_task_reassigned(
                task=task,
                reassigned_by=actor,
                previous_assignee=previous_assignee,
            )

    return task


def update_task_status(
    *,
    task: StaffTask,
    new_status: str,
    actor: Any,
) -> StaffTask:
    """Transition task status, enforcing lifecycle rules and dispatching notifications."""
    if not can_user_update_status(actor, task, new_status):
        raise PermissionDenied(
            f"You cannot change task #{task.pk} status from '{task.status}' to '{new_status}'."
        )

    old_status = task.status
    if old_status == new_status:
        return task

    with transaction.atomic():
        task.status = new_status

        if new_status == StaffTaskStatus.COMPLETED:
            task.completed_at = timezone.now()
            task.completed_by = actor
            task.progress_percent = 100
        elif old_status == StaffTaskStatus.COMPLETED and new_status == StaffTaskStatus.OPEN:
            # Reopening
            task.completed_at = None
            task.completed_by = None

        task.save()

        action_name = "status_changed"
        if new_status == StaffTaskStatus.COMPLETED:
            action_name = "completed"
        elif new_status == StaffTaskStatus.CANCELLED:
            action_name = "cancelled"
        elif (
            old_status in {StaffTaskStatus.COMPLETED, StaffTaskStatus.CANCELLED}
            and new_status == StaffTaskStatus.OPEN
        ):
            action_name = "reopened"

        StaffTaskActivity.objects.create(
            task=task,
            actor=actor,
            action=action_name,
            old_value=old_status,
            new_value=new_status,
            details=f"Status updated from {old_status} to {new_status}.",
        )

        notify_task_status_changed(
            task=task,
            changed_by=actor,
            old_status=old_status,
            new_status=new_status,
        )

    return task


def update_task_progress(
    *,
    task: StaffTask,
    progress_percent: int,
    actor: Any,
) -> StaffTask:
    """Update progress percentage (0 - 100)."""
    if not can_user_update_progress(actor, task):
        raise PermissionDenied("You do not have permission to update progress on this task.")

    if not (0 <= progress_percent <= 100):
        raise ValidationError("Progress must be between 0 and 100.")

    old_progress = task.progress_percent
    if old_progress == progress_percent:
        return task

    with transaction.atomic():
        task.progress_percent = progress_percent
        # Auto-advance from OPEN to IN_PROGRESS if progress > 0
        if task.status == StaffTaskStatus.OPEN and progress_percent > 0:
            task.status = StaffTaskStatus.IN_PROGRESS
        # Auto-complete if 100%
        if progress_percent == 100 and task.status != StaffTaskStatus.COMPLETED:
            task.status = StaffTaskStatus.COMPLETED
            task.completed_at = timezone.now()
            task.completed_by = actor

        task.save()

        StaffTaskActivity.objects.create(
            task=task,
            actor=actor,
            action="progress_updated",
            old_value=f"{old_progress}%",
            new_value=f"{progress_percent}%",
            details=f"Progress updated to {progress_percent}%.",
        )

    return task


def add_task_comment(
    *,
    task: StaffTask,
    author: Any,
    comment_text: str,
) -> StaffTaskComment:
    """Add an operational note/comment to the task and notify participants."""
    if not can_user_comment(author, task):
        raise PermissionDenied("You do not have permission to comment on this task.")

    text = comment_text.strip() if comment_text else ""
    if not text:
        raise ValidationError({"comment": "Comment text cannot be empty."})

    with transaction.atomic():
        comment = StaffTaskComment.objects.create(
            task=task,
            author=author,
            comment=text,
        )

        StaffTaskActivity.objects.create(
            task=task,
            actor=author,
            action="comment_added",
            details=f"Added comment: {text[:100]}",
        )

        notify_task_comment(task=task, author=author, comment_snippet=text)

    return comment


# --------------------------------------------------------------------------------------
# Queries, Filtering & Metrics
# --------------------------------------------------------------------------------------


def get_filtered_tasks(
    user: Any,
    *,
    status: str | None = None,
    priority: str | None = None,
    assignee_id: int | None = None,
    scope: str = "all",
    search_query: str | None = None,
    due_filter: str | None = None,
) -> QuerySet[StaffTask]:
    """Retrieve filtered, indexed tasks with eager loading to prevent N+1 queries.

    Enforces permission scoping:
    - Managers/Owners can view team-wide or filtered scopes.
    - Other staff can view tasks assigned to them, created by them, or unassigned team tasks.
    """
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return StaffTask.objects.none()

    qs = StaffTask.objects.select_related("creator", "assignee", "completed_by").order_by(
        "-created_at"
    )

    has_manage = user.is_superuser or has_staff_capability(user, TASKS_MANAGE)

    # Scoping
    if scope == "my":
        qs = qs.filter(assignee=user)
    elif scope == "created":
        qs = qs.filter(creator=user)
    elif scope == "unassigned":
        qs = qs.filter(assignee__isnull=True)
    elif not has_manage:
        # Non-managers: view tasks assigned to them or created by them by default
        qs = qs.filter(Q(assignee=user) | Q(creator=user))

    # Filters
    if status:
        qs = qs.filter(status=status)

    if priority:
        qs = qs.filter(priority=priority)

    if assignee_id:
        qs = qs.filter(assignee_id=assignee_id)

    if search_query:
        q = search_query.strip()
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(description__icontains=q)
            | Q(related_object_label__icontains=q)
        )

    now = timezone.now()
    if due_filter == "overdue":
        qs = qs.filter(
            due_date__lt=now,
            status__in=[StaffTaskStatus.OPEN, StaffTaskStatus.IN_PROGRESS, StaffTaskStatus.BLOCKED],
        )
    elif due_filter == "due_soon":
        qs = qs.filter(
            due_date__gte=now,
            due_date__lte=now + timedelta(hours=24),
            status__in=[StaffTaskStatus.OPEN, StaffTaskStatus.IN_PROGRESS, StaffTaskStatus.BLOCKED],
        )

    return qs


def get_staff_task_metrics(user: Any) -> dict[str, int]:
    """Return key task counts for staff dashboard cards."""
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return {
            "my_open_tasks": 0,
            "tasks_due_soon": 0,
            "overdue_tasks": 0,
            "unassigned_tasks": 0,
            "all_open_tasks": 0,
        }

    now = timezone.now()
    active_statuses = [StaffTaskStatus.OPEN, StaffTaskStatus.IN_PROGRESS, StaffTaskStatus.BLOCKED]

    my_open = StaffTask.objects.filter(assignee=user, status__in=active_statuses).count()

    my_due_soon = StaffTask.objects.filter(
        assignee=user,
        status__in=active_statuses,
        due_date__gte=now,
        due_date__lte=now + timedelta(hours=24),
    ).count()

    my_overdue = StaffTask.objects.filter(
        assignee=user,
        status__in=active_statuses,
        due_date__lt=now,
    ).count()

    has_manage = user.is_superuser or has_staff_capability(user, TASKS_MANAGE)
    unassigned = (
        StaffTask.objects.filter(assignee__isnull=True, status__in=active_statuses).count()
        if has_manage
        else 0
    )
    all_open = StaffTask.objects.filter(status__in=active_statuses).count() if has_manage else 0

    return {
        "my_open_tasks": my_open,
        "tasks_due_soon": my_due_soon,
        "overdue_tasks": my_overdue,
        "unassigned_tasks": unassigned,
        "all_open_tasks": all_open,
    }


# --------------------------------------------------------------------------------------
# Deadline & Exception Sweeper
# --------------------------------------------------------------------------------------


def sweep_approaching_and_overdue_tasks() -> dict[str, int]:
    """Sweep active tasks with approaching or missed deadlines and dispatch reminders.

    Idempotent: uses deterministic date-based idempotency keys so running hourly/daily
    will not send duplicate notifications on the same date.
    """
    now = timezone.now()
    active_statuses = [StaffTaskStatus.OPEN, StaffTaskStatus.IN_PROGRESS, StaffTaskStatus.BLOCKED]

    overdue_tasks = StaffTask.objects.filter(
        status__in=active_statuses,
        due_date__lt=now,
        assignee__isnull=False,
    ).select_related("assignee")

    due_soon_tasks = StaffTask.objects.filter(
        status__in=active_statuses,
        due_date__gte=now,
        due_date__lte=now + timedelta(hours=24),
        assignee__isnull=False,
    ).select_related("assignee")

    overdue_notified = 0
    for task in overdue_tasks:
        if notify_task_deadline_reminder(task=task, kind="overdue"):
            overdue_notified += 1

    due_soon_notified = 0
    for task in due_soon_tasks:
        if notify_task_deadline_reminder(task=task, kind="due_soon"):
            due_soon_notified += 1

    return {
        "overdue_scanned": overdue_tasks.count(),
        "overdue_notified": overdue_notified,
        "due_soon_scanned": due_soon_tasks.count(),
        "due_soon_notified": due_soon_notified,
    }
