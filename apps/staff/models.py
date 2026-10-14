"""Models for FLASHWEAR Staff Tasks, Collaboration & Activity History (Phase 63)."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class StaffTaskStatus(models.TextChoices):
    OPEN = "open", _("Open")
    IN_PROGRESS = "in_progress", _("In Progress")
    BLOCKED = "blocked", _("Blocked")
    COMPLETED = "completed", _("Completed")
    CANCELLED = "cancelled", _("Cancelled")


class StaffTaskPriority(models.TextChoices):
    LOW = "low", _("Low")
    NORMAL = "normal", _("Normal")
    HIGH = "high", _("High")
    URGENT = "urgent", _("Urgent")


class StaffTaskRelatedType(models.TextChoices):
    NONE = "", _("None")
    ORDER = "order", _("Order")
    SHIPMENT = "shipment", _("Shipment / Delivery")
    VARIANT = "variant", _("Inventory Variant / Discrepancy")
    SUPPORT_TICKET = "support_ticket", _("Support Ticket")


class StaffTask(models.Model):
    """A unit of operational work assigned to and tracked by staff members."""

    VALID_TRANSITIONS: dict[str, set[str]] = {
        StaffTaskStatus.OPEN: {
            StaffTaskStatus.IN_PROGRESS,
            StaffTaskStatus.BLOCKED,
            StaffTaskStatus.COMPLETED,
            StaffTaskStatus.CANCELLED,
        },
        StaffTaskStatus.IN_PROGRESS: {
            StaffTaskStatus.OPEN,
            StaffTaskStatus.BLOCKED,
            StaffTaskStatus.COMPLETED,
            StaffTaskStatus.CANCELLED,
        },
        StaffTaskStatus.BLOCKED: {
            StaffTaskStatus.OPEN,
            StaffTaskStatus.IN_PROGRESS,
            StaffTaskStatus.COMPLETED,
            StaffTaskStatus.CANCELLED,
        },
        StaffTaskStatus.COMPLETED: {
            StaffTaskStatus.OPEN,  # Reopened by manager or creator
        },
        StaffTaskStatus.CANCELLED: {
            StaffTaskStatus.OPEN,  # Reopened by manager or creator
        },
    }

    title = models.CharField(_("title"), max_length=255)
    description = models.TextField(_("description"), blank=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=StaffTaskStatus.choices,
        default=StaffTaskStatus.OPEN,
        db_index=True,
    )
    priority = models.CharField(
        _("priority"),
        max_length=20,
        choices=StaffTaskPriority.choices,
        default=StaffTaskPriority.NORMAL,
        db_index=True,
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_staff_tasks",
        verbose_name=_("creator"),
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_staff_tasks",
        verbose_name=_("assignee"),
        db_index=True,
    )
    due_date = models.DateTimeField(
        _("due date"),
        null=True,
        blank=True,
        db_index=True,
    )
    progress_percent = models.PositiveSmallIntegerField(
        _("progress percentage"),
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    # Linked business object references (avoiding direct customer PII)
    related_object_type = models.CharField(
        _("related object type"),
        max_length=32,
        choices=StaffTaskRelatedType.choices,
        default=StaffTaskRelatedType.NONE,
        blank=True,
        db_index=True,
    )
    related_object_id = models.PositiveIntegerField(
        _("related object id"),
        null=True,
        blank=True,
        db_index=True,
    )
    related_object_label = models.CharField(
        _("related object label"),
        max_length=255,
        blank=True,
    )

    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_staff_tasks",
        verbose_name=_("completed by"),
    )

    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("staff task")
        verbose_name_plural = _("staff tasks")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "priority"], name="staff_task_status_prio_idx"),
            models.Index(fields=["assignee", "status"], name="staff_task_assign_stat_idx"),
            models.Index(fields=["due_date", "status"], name="staff_task_due_stat_idx"),
        ]

    def __str__(self) -> str:
        return f"Task #{self.pk}: {self.title} ({self.get_status_display()})"

    @property
    def is_terminal(self) -> bool:
        return self.status in {StaffTaskStatus.COMPLETED, StaffTaskStatus.CANCELLED}

    @property
    def is_overdue(self) -> bool:
        if not self.due_date or self.is_terminal:
            return False
        return self.due_date < timezone.now()

    @property
    def is_due_soon(self) -> bool:
        if not self.due_date or self.is_terminal:
            return False
        now = timezone.now()
        return now <= self.due_date <= now + timedelta(hours=24)

    def can_transition_to(self, new_status: str) -> bool:
        if self.status == new_status:
            return True
        allowed = self.VALID_TRANSITIONS.get(self.status, set())
        return new_status in allowed


class StaffTaskComment(models.Model):
    """Operational comment or progress note on a staff task."""

    task = models.ForeignKey(
        StaffTask,
        on_delete=models.CASCADE,
        related_name="comments",
        verbose_name=_("task"),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_task_comments",
        verbose_name=_("author"),
    )
    comment = models.TextField(_("comment"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("staff task comment")
        verbose_name_plural = _("staff task comments")
        ordering = ["created_at"]

    def __str__(self) -> str:
        author_name = self.author.email if self.author else "System"
        return f"Comment by {author_name} on Task #{self.task_id}"


class StaffTaskActivity(models.Model):
    """Audit log entry capturing task lifecycle events and changes."""

    task = models.ForeignKey(
        StaffTask,
        on_delete=models.CASCADE,
        related_name="activities",
        verbose_name=_("task"),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_task_activities",
        verbose_name=_("actor"),
    )
    action = models.CharField(_("action"), max_length=64, db_index=True)
    old_value = models.CharField(_("old value"), max_length=255, blank=True)
    new_value = models.CharField(_("new value"), max_length=255, blank=True)
    details = models.TextField(_("details"), blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("staff task activity")
        verbose_name_plural = _("staff task activities")
        ordering = ["created_at"]

    def __str__(self) -> str:
        actor_name = self.actor.email if self.actor else "System"
        return f"{self.action} on Task #{self.task_id} by {actor_name}"
