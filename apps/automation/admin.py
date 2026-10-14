"""Django admin configuration for Business Automation (Phase 56)."""

from __future__ import annotations

from django.contrib import admin

from apps.automation.models import AutomationExecutionLog, AutomationRule


@admin.register(AutomationRule)
class AutomationRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "trigger_type",
        "action_type",
        "is_active",
        "last_status",
        "last_triggered_at",
        "consecutive_failures",
    )
    list_filter = ("is_active", "trigger_type", "action_type", "last_status")
    search_fields = ("name", "description")
    readonly_fields = (
        "last_triggered_at",
        "last_status",
        "consecutive_failures",
        "created_at",
        "updated_at",
    )


@admin.register(AutomationExecutionLog)
class AutomationExecutionLogAdmin(admin.ModelAdmin):
    list_display = (
        "rule",
        "trigger_event",
        "status",
        "items_evaluated",
        "items_affected",
        "duration_ms",
        "executed_at",
    )
    list_filter = ("status", "trigger_event", "executed_at")
    search_fields = ("rule__name", "target_repr", "idempotency_key", "error_summary")
    readonly_fields = (
        "rule",
        "trigger_event",
        "status",
        "idempotency_key",
        "target_repr",
        "items_evaluated",
        "items_affected",
        "details",
        "error_summary",
        "duration_ms",
        "executed_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
