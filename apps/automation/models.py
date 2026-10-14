"""Automation Models (Phase 56): Rules and Execution History.

Provides configurable business automations with strict validation, allowlists,
and append-only execution records.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.automation.constants import (
    ActionType,
    ExecutionStatus,
    TriggerType,
)
from apps.automation.validators import validate_rule_config

__all__ = ["AutomationExecutionLog", "AutomationRule"]


class AutomationRule(models.Model):
    """A configured business automation rule."""

    name = models.CharField(
        _("rule name"),
        max_length=120,
        unique=True,
        help_text=_("Human-readable name identifying this automation rule."),
    )
    description = models.TextField(
        _("description"),
        blank=True,
        help_text=_("Explanation of when and why this rule executes."),
    )
    trigger_type = models.CharField(
        _("trigger type"),
        max_length=64,
        choices=TriggerType.choices,
        db_index=True,
        help_text=_("Event or scheduled condition that triggers this automation."),
    )
    conditions = models.JSONField(
        _("conditions"),
        default=dict,
        blank=True,
        help_text=_("Validated condition parameters (e.g. threshold, SLA hours)."),
    )
    action_type = models.CharField(
        _("action type"),
        max_length=64,
        choices=ActionType.choices,
        db_index=True,
        help_text=_("Approved domain action executed when conditions are met."),
    )
    action_config = models.JSONField(
        _("action configuration"),
        default=dict,
        blank=True,
        help_text=_("Validated action parameters (e.g. staff capability, templates)."),
    )
    is_active = models.BooleanField(
        _("is active"),
        default=True,
        db_index=True,
        help_text=_("Whether this rule evaluates automatically."),
    )
    cooldown_minutes = models.PositiveIntegerField(
        _("cooldown (minutes)"),
        default=60,
        help_text=_(
            "Minimum minutes between repeated actions on the same item to prevent alert floods."
        ),
    )
    max_retries = models.PositiveIntegerField(
        _("max retries"),
        default=3,
        help_text=_("Maximum retry attempts before pausing the rule."),
    )
    last_triggered_at = models.DateTimeField(
        _("last triggered"),
        null=True,
        blank=True,
    )
    last_status = models.CharField(
        _("last status"),
        max_length=20,
        choices=ExecutionStatus.choices,
        blank=True,
        default="",
    )
    consecutive_failures = models.PositiveIntegerField(
        _("consecutive failures"),
        default=0,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="automation_rules",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(_("created"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("updated"), auto_now=True)

    class Meta:
        verbose_name = _("automation rule")
        verbose_name_plural = _("automation rules")
        ordering = ("-is_active", "name")

    def __str__(self) -> str:
        status = "Active" if self.is_active else "Disabled"
        return f"{self.name} ({status})"

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        validate_rule_config(
            trigger_type=self.trigger_type,
            action_type=self.action_type,
            conditions=self.conditions,
            action_config=self.action_config,
        )


class AutomationExecutionLog(models.Model):
    """An execution record for an automation evaluation."""

    rule = models.ForeignKey(
        AutomationRule,
        on_delete=models.CASCADE,
        related_name="logs",
        verbose_name=_("rule"),
    )
    trigger_event = models.CharField(
        _("trigger event"),
        max_length=64,
        db_index=True,
    )
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=ExecutionStatus.choices,
        db_index=True,
    )
    idempotency_key = models.CharField(
        _("idempotency key"),
        max_length=255,
        db_index=True,
        blank=True,
    )
    target_repr = models.CharField(
        _("target representation"),
        max_length=255,
        blank=True,
    )
    items_evaluated = models.PositiveIntegerField(
        _("items evaluated"),
        default=0,
    )
    items_affected = models.PositiveIntegerField(
        _("items affected"),
        default=0,
    )
    details = models.JSONField(
        _("details"),
        default=dict,
        blank=True,
    )
    error_summary = models.TextField(
        _("error summary"),
        blank=True,
    )
    duration_ms = models.PositiveIntegerField(
        _("duration (ms)"),
        default=0,
    )
    executed_at = models.DateTimeField(
        _("executed at"),
        auto_now_add=True,
        db_index=True,
    )

    class Meta:
        verbose_name = _("automation execution log")
        verbose_name_plural = _("automation execution logs")
        ordering = ("-executed_at", "-pk")
        indexes = [
            models.Index(fields=["rule", "-executed_at"], name="auto_log_rule_idx"),
            models.Index(fields=["status", "-executed_at"], name="auto_log_status_idx"),
            models.Index(fields=["idempotency_key", "-executed_at"], name="auto_log_key_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.rule.name} - {self.status} at {self.executed_at:%Y-%m-%d %H:%M:%S}"
