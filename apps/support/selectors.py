"""Read-side selectors for FLASH Support (Phase 15).

Where a query happens decides what it may contain:

* :func:`user_tickets` / :func:`user_messages` are scoped by user at the top, so a caller
  cannot forget it and a template cannot render somebody else's transcript;
* :func:`staff_tickets` / :func:`staff_messages` are only reachable from views that have
  already proved the agent capability -- a selector is not a permission check;
* every list is eager-loaded with one fixed plan, so a page of twenty tickets is a handful
  of queries rather than a hundred (the query-count tests pin this).
"""

from __future__ import annotations

from django.db.models import Count, Q, QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _

from apps.support.models import SupportTicket
from apps.support.permissions import SUPPORT_AGENT, has_capability

__all__ = [
    "desk_counts",
    "staff_messages",
    "staff_ticket",
    "staff_tickets",
    "status_choices_for",
    "status_label",
    "ticket_events",
    "user_messages",
    "user_ticket",
    "user_tickets",
]

# Everything a list or detail view might dereference, joined in one query so a ticket row
# never fires a second query per reference.
TICKET_RELATED = (
    "customer",
    "assigned_to",
    "order",
    "payment",
    "shipment",
    "product",
    "review",
    "loop_item",
    "quest",
)

OPEN_STATUSES = (
    SupportTicket.Status.OPEN,
    SupportTicket.Status.IN_PROGRESS,
    SupportTicket.Status.WAITING_FOR_CUSTOMER,
    SupportTicket.Status.WAITING_INTERNAL,
    SupportTicket.Status.ESCALATED,
)

# Explicit, not left to Meta: an annotated queryset is a GROUP BY query, and Django
# deliberately ignores a model's default ordering there -- which is exactly how page two
# starts repeating rows. ``-pk`` breaks ties so two tickets written in the same
# microsecond still paginate stably.
TICKET_ORDERING = ("-created_at", "-pk")


def user_tickets(user, *, status: str = "") -> QuerySet:
    """The signed-in customer's tickets, newest first, in one query."""
    qs = (
        SupportTicket.objects.filter(customer=user)
        .select_related(*TICKET_RELATED)
        .annotate(message_count=Count("messages", distinct=True))
        .order_by(*TICKET_ORDERING)
    )
    if status:
        qs = qs.filter(status=status)
    return qs


def user_ticket(user, number: str) -> SupportTicket:
    """One of the customer's tickets.

    Scoping by customer at the queryset level means another customer's ticket number does
    not 403 -- it simply does not exist as far as this caller is concerned.
    """
    return get_object_or_404(
        SupportTicket.objects.filter(customer=user)
        .select_related(*TICKET_RELATED)
        .annotate(message_count=Count("messages", distinct=True)),
        number=number,
    )


def user_messages(ticket: SupportTicket) -> QuerySet:
    """What the customer is allowed to read: everything that is not internal.

    Attachments ride along as a prefetch so the transcript costs one extra query for the
    whole page rather than one per message.
    """
    return (
        ticket.messages.filter(is_internal=False)
        .select_related("author")
        .prefetch_related("attachments")
    )


def staff_tickets(
    *,
    status: str = "",
    category: str = "",
    q: str = "",
    assigned_to=None,
) -> QuerySet:
    """The desk queue with the same eager-loading plan as the customer list."""
    qs = (
        SupportTicket.objects.select_related(*TICKET_RELATED)
        .annotate(message_count=Count("messages", distinct=True))
        .order_by(*TICKET_ORDERING)
    )
    if status:
        qs = qs.filter(status=status)
    if category:
        qs = qs.filter(category=category)
    if assigned_to is not None:
        qs = qs.filter(assigned_to=assigned_to)
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(Q(number__icontains=needle) | Q(subject__icontains=needle))
    return qs


def staff_ticket(number: str) -> SupportTicket:
    """One ticket for the desk. The caller has already proved the capability."""
    return get_object_or_404(
        SupportTicket.objects.select_related(*TICKET_RELATED).annotate(
            message_count=Count("messages", distinct=True)
        ),
        number=number,
    )


def staff_messages(ticket: SupportTicket) -> QuerySet:
    """The full transcript, internal notes included."""
    return ticket.messages.all().select_related("author").prefetch_related("attachments")


def ticket_events(ticket: SupportTicket) -> QuerySet:
    return ticket.events.all().select_related("actor")


def desk_counts(user) -> dict[str, int]:
    """Queue counters for the staff dashboard header -- one aggregate, not six counts."""
    if not has_capability(user, SUPPORT_AGENT):
        return {}
    counts = SupportTicket.objects.aggregate(
        total=Count("pk"),
        open=Count("pk", filter=Q(status=SupportTicket.Status.OPEN)),
        in_progress=Count("pk", filter=Q(status=SupportTicket.Status.IN_PROGRESS)),
        waiting_customer=Count("pk", filter=Q(status=SupportTicket.Status.WAITING_FOR_CUSTOMER)),
        waiting_internal=Count("pk", filter=Q(status=SupportTicket.Status.WAITING_INTERNAL)),
        escalated=Count("pk", filter=Q(status=SupportTicket.Status.ESCALATED)),
        resolved=Count("pk", filter=Q(status=SupportTicket.Status.RESOLVED)),
        unassigned=Count(
            "pk",
            filter=Q(assigned_to__isnull=True) & Q(status__in=OPEN_STATUSES),
        ),
    )
    return counts


def status_choices_for(user) -> list[tuple[str, str]]:
    """Statuses this actor may move a ticket to -- the form's option list.

    Deriving the list from the capability and the graph means the form can never offer an
    action the service would refuse.
    """
    if has_capability(user, SUPPORT_AGENT):
        return list(SupportTicket.Status.choices)
    return [
        (value, label)
        for value, label in SupportTicket.Status.choices
        if value in SupportTicket.CUSTOMER_TARGETS
    ]


def status_label(value: str) -> str:
    """Display label for a stored status value (used when rendering event history)."""
    try:
        return SupportTicket.Status(value).label
    except ValueError:
        return str(value) or _("Unknown")
