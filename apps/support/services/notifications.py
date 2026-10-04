"""Ticket notifications (Phase 15).

Email only. There is no SMS, no push and no third-party ticketing integration in this
phase, which keeps the delivery path inside the project's existing mailer
(``apps.accounts.mailer.send_account_email``) and its single swap point for a hosted
provider later.

The policy, in one place:

* the **customer** hears about public agent replies and about a ticket being resolved,
  escalated or reassigned -- never about an internal note;
* **agents** hear about a new ticket and about customer replies; **managers** additionally
  hear about escalations;
* a notification failure never fails the write that triggered it. A ticket the desk could
  not email about still exists and is still correct -- the same contract as the signal
  handlers elsewhere in the project: log it, keep the state, move on.
"""

from __future__ import annotations

import logging

from django.urls import reverse

from apps.accounts.mailer import send_account_email
from apps.support.models import SupportTicket, SupportTicketEvent
from apps.support.permissions import (
    SUPPORT_AGENT,
    SUPPORT_MANAGER,
    has_capability,
    staff_with_capability,
)
from apps.support.services import events

logger = logging.getLogger("flashwear.support.notifications")

__all__ = ["notify_agent", "notify_customer", "notify_managers", "notify_staff"]

SUBJECT_TEMPLATE = "support/email/notification_subject.txt"
TEXT_TEMPLATE = "support/email/notification.txt"
HTML_TEMPLATE = "support/email/notification.html"


def _path(ticket: SupportTicket, *, staff_view: bool) -> str:
    name = "support:staff-ticket-detail" if staff_view else "support:ticket-detail"
    try:
        return reverse(name, args=[ticket.number])
    except Exception:  # a missing route must never break a write
        logger.exception("Could not reverse %s for ticket %s", name, ticket.number)
        return "/support/"


def _send(
    ticket: SupportTicket,
    recipient: str,
    *,
    headline: str,
    staff_view: bool,
    request=None,
) -> bool:
    context = {
        "ticket": ticket,
        "headline": headline,
        "ticket_path": _path(ticket, staff_view=staff_view),
        "staff_view": staff_view,
    }
    try:
        return bool(
            send_account_email(
                subject_template=SUBJECT_TEMPLATE,
                text_template=TEXT_TEMPLATE,
                html_template=HTML_TEMPLATE,
                context=context,
                recipient=recipient,
                request=request,
                fail_silently=True,
            )
        )
    except Exception:  # delivery must never roll back the conversation
        logger.exception(
            "Support notification failed for ticket %s -> %s", ticket.number, recipient
        )
        return False


def notify_customer(ticket: SupportTicket, *, headline: str, request=None, actor=None) -> int:
    """Email the ticket's customer. Returns 1 when the backend accepted the message."""
    customer = ticket.customer
    if customer is None or not customer.is_active or not customer.email:
        return 0
    if not _send(ticket, customer.email, headline=headline, staff_view=False, request=request):
        return 0
    events.record(
        ticket,
        SupportTicketEvent.EventType.NOTIFICATION_SENT,
        actor=actor or customer,
        new=headline,
        metadata={"audience": "customer"},
    )
    return 1


def notify_staff(
    ticket: SupportTicket,
    *,
    headline: str,
    capability: str = SUPPORT_AGENT,
    exclude=None,
    request=None,
    actor=None,
) -> int:
    """Email every capable, active agent (or manager). Returns the number of recipients."""
    excluded = {exclude.pk} if exclude is not None else set()
    count = 0
    for agent in staff_with_capability(capability):
        if agent.pk in excluded or not agent.email:
            continue
        if _send(ticket, agent.email, headline=headline, staff_view=True, request=request):
            count += 1
    if count:
        events.record(
            ticket,
            SupportTicketEvent.EventType.NOTIFICATION_SENT,
            actor=actor,
            new=headline,
            metadata={"audience": "staff", "recipients": count, "capability": capability},
        )
    return count


def notify_managers(
    ticket: SupportTicket, *, headline: str, exclude=None, request=None, actor=None
) -> int:
    """Escalation copy: managers only, so an escalation is not a broadcast to the floor."""
    return notify_staff(
        ticket,
        headline=headline,
        capability=SUPPORT_MANAGER,
        exclude=exclude,
        request=request,
        actor=actor,
    )


def notify_agent(ticket: SupportTicket, agent, *, headline: str, request=None, actor=None) -> int:
    """Email one specific agent (the assignee), rather than the whole floor."""
    if agent is None or not agent.is_active or not agent.email:
        return 0
    if not has_capability(agent, SUPPORT_AGENT):
        return 0
    if not _send(ticket, agent.email, headline=headline, staff_view=True, request=request):
        return 0
    events.record(
        ticket,
        SupportTicketEvent.EventType.NOTIFICATION_SENT,
        actor=actor,
        new=headline,
        metadata={"audience": "agent", "capability": SUPPORT_AGENT},
    )
    return 1
