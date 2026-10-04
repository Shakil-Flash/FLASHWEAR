"""Serializers for the support API (Phase 15).

The rules that matter are the ones expressed by *absence*: the create payloads declare
``subject``, ``category``, ``description`` and a reference handle or two, and nothing else.
No ``status``, no ``priority``, no ``source``, no ``customer`` -- those are decided by the
service and by whoever holds the agent capability, so there is no field for a client to
mass-assign.

Reference handles mirror the HTML form: an order by its number, a product or quest by its
slug, everything else by id. Ownership is proved by
:func:`apps.support.services.references.resolve_for_customer`, never by the serializer.
"""

from __future__ import annotations

from django.urls import reverse
from rest_framework import serializers

from apps.support.models import SupportAttachment, SupportMessage, SupportTicket
from apps.support.services.references import CUSTOMER_REFERENCE_FIELDS

__all__ = [
    "AssignSerializer",
    "EscalateSerializer",
    "LinkSerializer",
    "MessageCreateSerializer",
    "PrioritySerializer",
    "StatusSerializer",
    "SupportAttachmentSerializer",
    "SupportMessageSerializer",
    "SupportTicketCreateSerializer",
    "SupportTicketSerializer",
]


class SupportAttachmentSerializer(serializers.ModelSerializer):
    """A download handle, never the storage path: ``url`` is our own authenticated route."""

    url = serializers.SerializerMethodField()
    filename = serializers.CharField(source="original_filename", read_only=True)

    class Meta:
        model = SupportAttachment
        fields = ("id", "filename", "content_type", "size", "url", "created_at")
        read_only_fields = fields

    def get_url(self, obj) -> str | None:
        request = self.context.get("request")
        path = reverse("support:attachment", args=[obj.pk])
        return request.build_absolute_uri(path) if request else path


class SupportMessageSerializer(serializers.ModelSerializer):
    """One transcript entry. ``is_internal`` only ever reaches a capable desk user --
    the queryset is scoped before it gets here, and the field is the belt to that braces."""

    author_name = serializers.SerializerMethodField()
    attachments = SupportAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = SupportMessage
        fields = (
            "id",
            "author_type",
            "author_name",
            "is_internal",
            "body",
            "created_at",
            "attachments",
        )
        read_only_fields = fields

    def get_author_name(self, obj) -> str:
        if obj.author is None:
            return "System"
        return obj.author.get_full_name() or obj.author.email


class SupportTicketSerializer(serializers.ModelSerializer):
    """A ticket as the API exposes it.

    ``assignee`` and ``customer_email`` are staff-only context: a customer's own ticket
    should not hand out the address of whoever picked it up.
    """

    assignee = serializers.SerializerMethodField()
    customer_email = serializers.SerializerMethodField()
    references = serializers.SerializerMethodField()
    message_count = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicket
        fields = (
            "number",
            "subject",
            "description",
            "category",
            "priority",
            "status",
            "source",
            "created_at",
            "updated_at",
            "first_response_at",
            "resolved_at",
            "closed_at",
            "assignee",
            "customer_email",
            "references",
            "message_count",
            "url",
        )
        read_only_fields = fields

    def _staff(self) -> bool:
        return bool(self.context.get("staff"))

    def get_assignee(self, obj) -> str | None:
        if not self._staff() or obj.assigned_to_id is None:
            return None
        return obj.assigned_to.email

    def get_customer_email(self, obj) -> str | None:
        if not self._staff():
            return None
        return obj.customer.email if obj.customer_id else None

    def get_references(self, obj) -> dict:
        refs = {
            key: getattr(obj, f"{key}_id", None)
            for key in (
                "order",
                "payment",
                "shipment",
                "product",
                "review",
                "loop_item",
                "quest",
            )
        }
        refs["creator_post_id"] = obj.creator_post_id
        return refs

    def get_message_count(self, obj) -> int:
        # Annotated by every selector; never a lazy count, which is what stops a list of
        # twenty tickets from becoming twenty-one queries.
        return int(getattr(obj, "message_count", 0) or 0)

    def get_url(self, obj) -> str:
        request = self.context.get("request")
        path = reverse("support:ticket-detail", args=[obj.number])
        return request.build_absolute_uri(path) if request else path


class SupportTicketCreateSerializer(serializers.Serializer):
    """``POST /api/v1/support/tickets/``. Deliberately narrow -- see the module docstring."""

    subject = serializers.CharField(max_length=200)
    category = serializers.ChoiceField(choices=SupportTicket.Category.choices)
    description = serializers.CharField(max_length=10_000)
    order = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    product = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    review = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    loop_item = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    quest = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def references(self) -> dict:
        return {
            key: self.validated_data.get(key)
            for key in CUSTOMER_REFERENCE_FIELDS
            if self.validated_data.get(key) not in (None, "")
        }


class MessageCreateSerializer(serializers.Serializer):
    """``POST .../messages/``. ``internal`` is accepted only so that it can be *ignored*
    for customers: the view passes it through solely when the actor holds the capability,
    and the database refuses the combination regardless."""

    body = serializers.CharField(max_length=10_000)
    internal = serializers.BooleanField(required=False, default=False, write_only=True)


class StatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=SupportTicket.Status.choices)


class PrioritySerializer(serializers.Serializer):
    priority = serializers.ChoiceField(choices=SupportTicket.Priority.choices)


class AssignSerializer(serializers.Serializer):
    """Blank ``agent_email`` releases the ticket back to the queue."""

    agent_email = serializers.EmailField(required=False, allow_blank=True, default="")


class EscalateSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=2000)
    target = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=("", "tier_2", "team_lead", "on_call"),
    )


class LinkSerializer(serializers.Serializer):
    """``null`` unlinks; an omitted key leaves the reference alone."""

    order = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    payment = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    shipment = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    product = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    review = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    loop_item = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    quest = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def references(self) -> dict:
        return dict(self.validated_data.items())
