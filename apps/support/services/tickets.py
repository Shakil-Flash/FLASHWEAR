"""Ticket lifecycle services (Phase 15).

Every rule that decides *whether* a ticket may change lives here, so the HTML desk, the JSON
API and the admin all obey one graph. The surface:

* :func:`open_ticket` -- create, with duplicate detection and the per-user rate limit;
* :func:`transition_ticket` -- the one place status is written, with the timestamps,
  notifications and audit row that follow from it;
* :func:`assign_ticket` / :func:`set_priority` / :func:`escalate_ticket` -- agent-only
  desk operations;
* :func:`link_references` -- point a ticket at a different row of another domain.

Support never writes to orders, payments, inventory or loyalty. If an operator needs a
refund, they attach the order and say so; that boundary is why this app can be granted to
junior staff without handing them the till.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.support.models import SupportMessage, SupportTicket, SupportTicketEvent
from apps.support.permissions import SUPPORT_AGENT, SUPPORT_MANAGER, has_capability
from apps.support.services import events, notifications, references
from apps.support.services.attachments import attach, validate_attachment
from apps.support.services.errors import (
    DuplicateTicketError,
    ForbiddenError,
    SupportError,
    TransitionError,
)
from apps.support.services.throttling import SupportThrottle

__all__ = [
    "assign_ticket",
    "clean_text",
    "escalate_ticket",
    "find_duplicate",
    "link_ticket_references",
    "open_ticket",
    "set_priority",
    "transition_ticket",
]

TERMINAL = (SupportTicket.Status.RESOLVED, SupportTicket.Status.CLOSED)
DUPLICATE_FIELDS = ("order", "payment", "product", "review", "loop_item", "quest")


def _require(actor, capability: str, message: str) -> None:
    if not has_capability(actor, capability):
        raise ForbiddenError(message)


def clean_text(value: str, *, field: str, max_length: int) -> str:
    text = (value or "").strip()
    if not text:
        raise SupportError(_("%s is required.") % field, code="support_invalid")
    if len(text) > max_length:
        raise SupportError(
            _("%s must be %(limit)d characters or fewer.") % {"limit": max_length},
            code="support_invalid",
        )
    return text


# =============================================================================
# Creation
# =============================================================================


def find_duplicate(customer, category: str, resolved: dict) -> SupportTicket | None:
    """An open ticket from the same customer about the same reference, if one exists.

    Deliberately narrow: without a reference there is nothing to compare, and comparing
    subjects would reject "where is my order" versus "where is my order" written differently
    by the same person on different days. Two tickets about *different* orders are two real
    problems, so the reference -- not the wording -- is the identity.
    """
    if customer is None:
        return None
    matching = [resolved[field] for field in DUPLICATE_FIELDS if resolved.get(field)]
    if not matching:
        return None
    clause = Q()
    for field in DUPLICATE_FIELDS:
        if resolved.get(field):
            clause |= Q(**{field: resolved[field]})
    return (
        SupportTicket.objects.filter(customer=customer, category=category)
        .filter(clause)
        .exclude(status__in=TERMINAL)
        .order_by("-created_at")
        .first()
    )


@transaction.atomic
def open_ticket(
    *,
    customer,
    subject: str,
    description: str,
    category: str,
    source: str = SupportTicket.Source.WEB,
    priority: str = SupportTicket.Priority.NORMAL,
    refs: dict | None = None,
    uploads=None,
    request=None,
) -> SupportTicket:
    """Open a ticket, its first customer message and its audit history.

    Validation happens in this order on purpose: rate limit (cheap, and the thing an
    abuser hits), then reference ownership, then attachment content -- so nothing is
    written until everything has passed. Files are validated *before* the ticket exists
    because storage writes are not transactional: a rejected upload must not leave an
    orphaned ticket, or an orphaned file on disk.
    """
    if category not in SupportTicket.Category.values:
        raise SupportError(_("That category does not exist."), code="support_invalid")
    if priority not in SupportTicket.Priority.values:
        raise SupportError(_("That priority does not exist."), code="support_invalid")

    subject = clean_text(subject, field=_("Subject"), max_length=200)
    description = clean_text(description, field=_("Message"), max_length=10_000)

    throttle = SupportThrottle()
    decision = throttle.check_open(customer)
    if decision.blocked:
        raise SupportError(
            _(
                "You have opened several tickets in a row. Please wait a little before "
                "starting another one."
            ),
            code="support_throttled",
        )

    resolved = references.resolve_for_customer(customer, **(refs or {}))

    upload_list = list(uploads or [])
    for upload in upload_list:
        validate_attachment(upload)

    duplicate = find_duplicate(customer, category, resolved)
    if duplicate is not None:
        raise DuplicateTicketError(
            _(
                "You already have an open ticket about this: %(number)s. "
                "Please continue the conversation there instead."
            )
            % {"number": duplicate.number},
            code="support_duplicate",
        )

    ticket = SupportTicket(
        customer=customer,
        subject=subject,
        description=description,
        category=category,
        priority=priority,
        source=source,
        **resolved,
    )
    ticket.save()
    message = SupportMessage.objects.create(
        ticket=ticket,
        author=customer,
        author_type=SupportMessage.AuthorType.CUSTOMER,
        body=description,
    )
    for upload in upload_list:
        attachment = attach(message, uploaded_by=customer, upload=upload)
        events.record(
            ticket,
            SupportTicketEvent.EventType.ATTACHMENT_ADDED,
            actor=customer,
            new=attachment.original_filename,
        )
    events.record(
        ticket,
        SupportTicketEvent.EventType.CREATED,
        actor=customer,
        new=ticket.status,
        metadata={"category": category, "source": source},
    )
    throttle.record_open(customer)
    notifications.notify_staff(
        ticket,
        headline=_("New ticket %(number)s: %(subject)s")
        % {"number": ticket.number, "subject": ticket.subject},
        actor=customer,
        request=request,
    )
    return ticket


# =============================================================================
# Transitions
# =============================================================================


def transition_ticket(
    ticket: SupportTicket,
    *,
    to_status: str,
    actor=None,
    notify: bool = True,
    request=None,
) -> SupportTicket:
    """Move ``ticket`` to ``to_status`` and write everything that follows from it.

    Three rules, all enforced here:

    1. a customer (anyone without the agent capability) may only reach
       :attr:`SupportTicket.CUSTOMER_TARGETS` -- close, or get the conversation moving
       again;
    2. the move has to exist on the graph, and the message names both states;
    3. timestamps follow the state: ``resolved_at`` and ``closed_at`` are set once, on the
       transition in, and cleared on the transition out, so "resolved" always means
       *currently* resolved.
    """
    if to_status not in SupportTicket.Status.values:
        raise SupportError(_("That status does not exist."), code="support_invalid")
    if to_status == ticket.status:
        return ticket

    if not has_capability(actor, SUPPORT_AGENT) and to_status not in SupportTicket.CUSTOMER_TARGETS:
        raise ForbiddenError(_("Only the support team can change a ticket to that status."))

    if not ticket.can_transition_to(to_status):
        raise TransitionError(
            _("A ticket cannot move from %(from)s to %(to)s.")
            % {"from": ticket.get_status_display(), "to": SupportTicket.Status(to_status).label},
            code="support_bad_transition",
        )

    previous = ticket.status
    now = timezone.now()
    ticket.status = to_status

    if to_status == SupportTicket.Status.RESOLVED and ticket.resolved_at is None:
        ticket.resolved_at = now
    if previous == SupportTicket.Status.RESOLVED and to_status != SupportTicket.Status.RESOLVED:
        ticket.resolved_at = None
    if to_status == SupportTicket.Status.CLOSED and ticket.closed_at is None:
        ticket.closed_at = now
    if previous == SupportTicket.Status.CLOSED and to_status != SupportTicket.Status.CLOSED:
        ticket.closed_at = None

    ticket.save(update_fields=["status", "resolved_at", "closed_at", "updated_at"])
    events.record(
        ticket,
        SupportTicketEvent.EventType.STATUS_CHANGED,
        actor=actor,
        old=previous,
        new=to_status,
    )

    if notify:
        _notify_status_change(ticket, to_status, actor=actor, request=request)
    return ticket


def _notify_status_change(ticket, to_status: str, *, actor=None, request=None) -> None:
    """Who hears about a status change, and who does not.

    The customer hears when something is *for them* (waiting on you, resolved, closed,
    escalated); the desk hears when a manager needs to look. An internal re-shuffle -- back
    to in-progress, out to the internal team -- emails nobody.
    """
    headline_by_status = {
        SupportTicket.Status.WAITING_FOR_CUSTOMER: _(
            "We need one more detail on ticket %(number)s"
        ),
        SupportTicket.Status.RESOLVED: _("Ticket %(number)s is resolved"),
        SupportTicket.Status.CLOSED: _("Ticket %(number)s is closed"),
        SupportTicket.Status.ESCALATED: _("Ticket %(number)s was escalated"),
    }
    template = headline_by_status.get(to_status)
    if template is not None:
        notifications.notify_customer(
            ticket,
            headline=template % {"number": ticket.number},
            request=request,
            actor=actor,
        )
    if to_status == SupportTicket.Status.ESCALATED:
        notifications.notify_managers(
            ticket,
            headline=_("Escalated: ticket %(number)s") % {"number": ticket.number},
            exclude=actor,
            request=request,
            actor=actor,
        )


def close_ticket(ticket: SupportTicket, *, actor=None, request=None) -> SupportTicket:
    """Close a ticket. Available to its customer and to the desk."""
    return transition_ticket(
        ticket, to_status=SupportTicket.Status.CLOSED, actor=actor, request=request
    )


def reopen_ticket(ticket: SupportTicket, *, actor=None, request=None) -> SupportTicket:
    """Reopen a closed ticket (``CLOSED -> IN_PROGRESS``)."""
    return transition_ticket(
        ticket, to_status=SupportTicket.Status.IN_PROGRESS, actor=actor, request=request
    )


# =============================================================================
# Desk operations
# =============================================================================


def assign_ticket(ticket: SupportTicket, *, actor, agent=None, request=None) -> SupportTicket:
    """Claim, reassign or release a ticket.

    An agent may take a ticket that nobody holds (or keep the one already theirs); moving a
    ticket off somebody else, handing it to a colleague, or releasing it is a manager action,
    which is what stops two agents quietly fighting over the same queue without a lead
    noticing.
    """
    _require(actor, SUPPORT_AGENT, _("Only the support team can assign tickets."))
    taking_own = (
        agent is not None and agent.pk == actor.pk and ticket.assigned_to_id in (None, actor.pk)
    )
    if not taking_own:
        _require(
            actor,
            SUPPORT_MANAGER,
            _("Only a support manager can reassign or unassign a ticket."),
        )
    if agent is not None and not has_capability(agent, SUPPORT_AGENT):
        raise SupportError(_("That user is not on the support desk."), code="support_bad_agent")

    previous = ticket.assigned_to
    ticket.assigned_to = agent
    ticket.assigned_at = timezone.now() if agent is not None else None
    ticket.save(update_fields=["assigned_to", "assigned_at", "updated_at"])

    if agent is None:
        events.record(
            ticket,
            SupportTicketEvent.EventType.UNASSIGNED,
            actor=actor,
            old=previous.email if previous else "",
        )
    else:
        events.record(
            ticket,
            SupportTicketEvent.EventType.ASSIGNED,
            actor=actor,
            old=previous.email if previous else "",
            new=agent.email,
        )
        notifications.notify_agent(
            ticket,
            agent,
            headline=_("Assigned to you: ticket %(number)s") % {"number": ticket.number},
            request=request,
            actor=actor,
        )
        notifications.notify_customer(
            ticket,
            headline=_("The support team is working on ticket %(number)s")
            % {"number": ticket.number},
            request=request,
            actor=actor,
        )
    return ticket


def set_priority(ticket: SupportTicket, *, actor, priority: str) -> SupportTicket:
    """Change urgency. Customers have no path here: priority is a desk estimate of impact,
    and letting the requester set it would make every ticket urgent."""
    _require(actor, SUPPORT_AGENT, _("Only the support team can change priority."))
    if priority not in SupportTicket.Priority.values:
        raise SupportError(_("That priority does not exist."), code="support_invalid")
    if priority == ticket.priority:
        return ticket
    previous = ticket.priority
    ticket.priority = priority
    ticket.save(update_fields=["priority", "updated_at"])
    events.record(
        ticket,
        SupportTicketEvent.EventType.PRIORITY_CHANGED,
        actor=actor,
        old=previous,
        new=priority,
    )
    return ticket


def escalate_ticket(
    ticket: SupportTicket,
    *,
    actor,
    reason: str,
    target: str = "",
    request=None,
) -> SupportTicket:
    """Escalate: move to ``ESCALATED``, remember why, tell the managers."""
    _require(actor, SUPPORT_MANAGER, _("Only a support manager can escalate a ticket."))
    reason = clean_text(reason, field=_("Escalation reason"), max_length=2000)

    previous = ticket.status
    transition_ticket(
        ticket, to_status=SupportTicket.Status.ESCALATED, actor=actor, request=request
    )
    ticket.escalation_reason = reason
    ticket.escalation_target = (target or "")[:20]
    ticket.escalated_by = actor
    ticket.escalated_at = timezone.now()
    ticket.save(
        update_fields=[
            "escalation_reason",
            "escalation_target",
            "escalated_by",
            "escalated_at",
            "updated_at",
        ]
    )
    events.record(
        ticket,
        SupportTicketEvent.EventType.ESCALATED,
        actor=actor,
        old=previous,
        new=ticket.escalation_target or SupportTicket.Status.ESCALATED,
        metadata={"reason": reason[:200]},
    )
    return ticket


def link_ticket_references(ticket: SupportTicket, *, actor, **refs) -> list[str]:
    """Attach (or clear) references on an existing ticket. Staff only."""
    _require(actor, SUPPORT_AGENT, _("Only the support team can link records."))
    changed = references.link_references(ticket, **refs)
    if not changed:
        return []
    ticket.save(update_fields=["updated_at", *changed])
    events.record(
        ticket,
        SupportTicketEvent.EventType.REFERENCE_LINKED,
        actor=actor,
        new=", ".join(changed),
    )
    return changed
