"""Admin for the notification layer (Phase 17).

Read-heavy on purpose: ``Notification`` rows are a delivery *record* -- editing one by
hand would let staff fabricate history (flip a failed send to sent) and make the back
office's own metrics lie. Staff action happens through the back office (retry with a real
audit trail), not through raw field edits; forms therefore expose almost nothing to write.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.notifications.models import (
    Notification,
    NotificationPreference,
    NotificationSubscription,
    UnsubscribeToken,
)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "user_email",
        "notification_type",
        "channel",
        "status",
        "attempts",
        "title",
    )
    list_filter = ("status", "channel", "notification_type", "category", "priority")
    search_fields = ("title", "body", "idempotency_key", "user__email")
    ordering = ("-created_at",)
    readonly_fields = (
        "user",
        "notification_type",
        "category",
        "channel",
        "title",
        "body",
        "status",
        "priority",
        "created_at",
        "updated_at",
        "scheduled_for",
        "sent_at",
        "read_at",
        "failed_at",
        "failure_reason",
        "attempts",
        "related_object_type",
        "related_object_id",
        "action_url",
        "metadata",
        "idempotency_key",
    )
    date_hierarchy = "created_at"

    @admin.display(description=_("user"))
    def user_email(self, obj) -> str:
        return obj.user.email

    def has_add_permission(self, request) -> bool:
        # Rows are created by the dispatcher; a hand-written row would bypass
        # idempotency and preferences, so there is nothing legitimate to add.
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # Retention is the sweeper's job; ad-hoc deletion would silently unsend history.
        return request.user.is_superuser


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user_email", "category", "email_enabled", "in_app_enabled", "updated_at")
    list_filter = ("category", "email_enabled", "in_app_enabled")
    search_fields = ("user__email",)
    ordering = ("user__email", "category")

    @admin.display(description=_("user"))
    def user_email(self, obj) -> str:
        return obj.user.email


@admin.register(NotificationSubscription)
class NotificationSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user_email", "notification_type", "related_object", "created_at")
    list_filter = ("notification_type", "related_object_type")
    search_fields = ("user__email",)
    ordering = ("-created_at",)

    @admin.display(description=_("user"))
    def user_email(self, obj) -> str:
        return obj.user.email

    @admin.display(description=_("object"))
    def related_object(self, obj) -> str:
        return f"{obj.related_object_type}:{obj.related_object_id}"


@admin.register(UnsubscribeToken)
class UnsubscribeTokenAdmin(admin.ModelAdmin):
    list_display = ("user_email", "created_at")
    search_fields = ("user__email", "token")
    ordering = ("-created_at",)
    readonly_fields = ("user", "token", "created_at")

    @admin.display(description=_("user"))
    def user_email(self, obj) -> str:
        return obj.user.email
