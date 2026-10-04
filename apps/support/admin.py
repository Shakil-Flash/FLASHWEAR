"""Admin for FLASH Support & Customer Care (Phase 15).

Operators *investigate* here; they do not work the queue here. Every model is registered
read-only, for the same reason the quests admin is: the rules that decide what may change
(live in :mod:`apps.support.services`) must have exactly one implementation, and an admin
form that writes ``status`` straight to the column would be a second one that skips the
transition graph, the timestamps, the notifications and the audit row.

Two details worth knowing:

* attachments never render Django's file widget. ``SupportAttachment.file`` has no public
  URL at all (see :mod:`apps.support.storage`), so asking it for ``.url`` would raise; the
  admin offers an authenticated download link instead;
* events refuse add, change and delete -- history that an operator can rewrite is not
  history.
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from apps.support.models import (
    SupportAttachment,
    SupportMessage,
    SupportTicket,
    SupportTicketEvent,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Base: rows can be read and searched, never written from here."""

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(SupportTicket)
class SupportTicketAdmin(ReadOnlyAdmin):
    list_display = (
        "number",
        "subject",
        "category",
        "priority",
        "status",
        "customer_email",
        "assigned_to",
        "created_at",
    )
    list_filter = ("status", "category", "priority", "source")
    search_fields = ("number", "subject", "description", "customer__email")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    fields = (
        "number",
        "customer",
        "subject",
        "description",
        "category",
        "priority",
        "status",
        "source",
        "assigned_to",
        "assigned_at",
        "first_response_at",
        "resolved_at",
        "closed_at",
        "last_customer_message_at",
        "last_agent_message_at",
        "escalation_reason",
        "escalation_target",
        "escalated_by",
        "escalated_at",
        "order",
        "payment",
        "shipment",
        "product",
        "review",
        "loop_item",
        "quest",
        "creator_post_id",
        "metadata",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields

    @admin.display(description="Customer")
    def customer_email(self, obj: SupportTicket) -> str:
        return obj.customer.email if obj.customer_id else "—"


@admin.register(SupportMessage)
class SupportMessageAdmin(ReadOnlyAdmin):
    list_display = ("ticket", "author_type", "is_internal", "preview", "created_at")
    list_filter = ("author_type", "is_internal")
    search_fields = ("body", "ticket__number", "author__email")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    fields = ("ticket", "author", "author_type", "is_internal", "body", "created_at")
    readonly_fields = fields

    @admin.display(description="Message")
    def preview(self, obj: SupportMessage) -> str:
        return obj.body[:80]


@admin.register(SupportAttachment)
class SupportAttachmentAdmin(ReadOnlyAdmin):
    """Read-only, and ``file`` is never a form field: it has no URL to hand out."""

    list_display = (
        "original_filename",
        "message_ticket",
        "content_type",
        "size",
        "created_at",
        "download",
    )
    search_fields = ("original_filename", "message__ticket__number")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    fields = (
        "message",
        "original_filename",
        "content_type",
        "size",
        "uploaded_by",
        "created_at",
        "download",
    )
    readonly_fields = fields

    @admin.display(description="Ticket")
    def message_ticket(self, obj: SupportAttachment) -> str:
        return obj.message.ticket.number

    @admin.display(description="Download")
    def download(self, obj: SupportAttachment) -> str:
        """An authenticated link. Guessing the id still requires being signed in as the
        owner or an agent -- the view enforces that, not this anchor."""
        url = reverse("support:attachment", args=[obj.pk])
        return format_html('<a href="{}">Open file</a>', url)


@admin.register(SupportTicketEvent)
class SupportTicketEventAdmin(ReadOnlyAdmin):
    """Append-only audit: no add, no change, no delete."""

    list_display = ("ticket", "event_type", "actor", "old_value", "new_value", "created_at")
    list_filter = ("event_type",)
    search_fields = ("ticket__number", "actor__email", "new_value")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    fields = ("ticket", "actor", "event_type", "old_value", "new_value", "metadata", "created_at")
    readonly_fields = fields

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
