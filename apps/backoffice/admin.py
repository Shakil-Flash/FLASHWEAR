"""Django admin registration for the back office's own model (Phase 16).

Only :class:`~apps.backoffice.models.AuditEvent` lives here. It is registered **read-only**
-- no add, no change, no delete -- because the append-only guarantee belongs in every
surface, not just the API: if an operator could edit an audit row from ``/admin/``, the
log would be evidence only until somebody with the admin opened it.

Everything else the back office shows is registered by the app that owns it.
"""

from __future__ import annotations

from django.contrib import admin

from apps.backoffice.models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """Browse the trail. There is nothing to create, change or delete."""

    list_display = ("created_at", "domain", "action", "object_type", "object_repr", "actor_label")
    list_filter = ("domain", "action")
    search_fields = ("object_id", "object_repr", "actor_label", "reason")
    ordering = ("-created_at",)
    readonly_fields = (
        "actor",
        "actor_label",
        "domain",
        "action",
        "object_type",
        "object_id",
        "object_repr",
        "reason",
        "metadata",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
