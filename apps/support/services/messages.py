"""Transcript services: replies and internal notes (Phase 15).

Two writers, one rule set:

* :func:`post_customer_message` -- only the ticket's own customer, only on a ticket that is
  not closed, always public, always rate-limited. It also drives the one status the customer
  owns by replying: ``WAITING_FOR_CUSTOMER -> IN_PROGRESS``.
* :func:`post_agent_message` -- only a holder of the agent capability. A public reply marks
  the first response, stamps the agent timestamp, and (unless the ticket is already
  resolved) parks the ticket on the customer; an **internal note** changes nothing the
  customer can see and emails nobody.

Attachments go through the same content rules as ticket creation, and a message is written
with its files in one transaction so a transcript never references bytes that were refused.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.support.models import SupportMessage, SupportTicket, SupportTicketEvent
from apps.support.permissions import SUPPORT_AGENT, has_capability
from apps.support.services import events, notifications
from apps.support.services.attachments import attach, validate_attachment
from apps.support.services.errors import ForbiddenError, SupportError
from apps.support.services.throttling import SupportThrottle
from apps.support.services.tickets import clean_text, transition_ticket

__all__ = ["post_agent_message", "post_customer_message"]

# Statuses a public agent reply parks on the customer. RESOLVED and CLOSED are excluded:
# an agent adding a footnote to a resolved ticket does not un-resolve it.
AGENT_REPLY_PARKS = (
    SupportTicket.Status.OPEN,
    SupportTicket.Status.IN_PROGRESS,
    SupportTicket.Status.WAITING_INTERNAL,
    SupportTicket.Status.ESCALATED,
)


def _require_open(ticket: SupportTicket) -> None:
    if ticket.status == SupportTicket.Status.CLOSED:
        raise SupportError(
            _("This ticket is closed. Reopen it to continue the conversation."),
            code="support_closed",
        )


def _validate_uploads(uploads) -> list:
    upload_list = list(uploads or [])
    for upload in upload_list:
        validate_attachment(upload)
    return upload_list


@transaction.atomic
def post_customer_message(
    ticket: SupportTicket,
    *,
    customer,
    body: str,
    uploads=None,
    request=None,
) -> SupportMessage:
    """The customer answers. Ownership, state and rate limit are all checked here."""
    if customer is None or ticket.customer_id != customer.pk:
        # The route already scopes by customer; this is the second lock on the same door.
        raise ForbiddenError(_("We could not verify your access to this ticket."))
    _require_open(ticket)
    body = clean_text(body, field=_("Message"), max_length=10_000)

    throttle = SupportThrottle()
    decision = throttle.check_message(customer)
    if decision.blocked:
        raise SupportError(
            _("You are posting messages faster than we can keep up. Please pause."),
            code="support_throttled",
        )

    upload_list = _validate_uploads(uploads)
    message = SupportMessage.objects.create(
        ticket=ticket,
        author=customer,
        author_type=SupportMessage.AuthorType.CUSTOMER,
        body=body,
    )
    for upload in upload_list:
        attachment = attach(message, uploaded_by=customer, upload=upload)
        events.record(
            ticket,
            SupportTicketEvent.EventType.ATTACHMENT_ADDED,
            actor=customer,
            new=attachment.original_filename,
        )

    ticket.last_customer_message_at = timezone.now()
    ticket.save(update_fields=["last_customer_message_at", "updated_at"])
    events.record(
        ticket, SupportTicketEvent.EventType.CUSTOMER_MESSAGE, actor=customer, new=message.pk
    )

    # Replying is how the customer answers a question we asked.
    if ticket.status == SupportTicket.Status.WAITING_FOR_CUSTOMER:
        transition_ticket(
            ticket,
            to_status=SupportTicket.Status.IN_PROGRESS,
            actor=customer,
            request=request,
        )

    throttle.record_message(customer)
    notifications.notify_staff(
        ticket,
        headline=_("Customer replied on ticket %(number)s") % {"number": ticket.number},
        request=request,
        actor=customer,
    )
    return message


@transaction.atomic
def post_agent_message(
    ticket: SupportTicket,
    *,
    actor,
    body: str,
    internal: bool = False,
    uploads=None,
    request=None,
) -> SupportMessage:
    """An agent replies publicly, or leaves a note the customer will never see."""
    if not has_capability(actor, SUPPORT_AGENT):
        raise ForbiddenError(_("Only the support team can reply on a ticket."))
    _require_open(ticket)
    body = clean_text(body, field=_("Message"), max_length=10_000)
    upload_list = _validate_uploads(uploads)

    message = SupportMessage.objects.create(
        ticket=ticket,
        author=actor,
        author_type=SupportMessage.AuthorType.AGENT,
        body=body,
        is_internal=internal,
    )
    for upload in upload_list:
        attachment = attach(message, uploaded_by=actor, upload=upload)
        events.record(
            ticket,
            SupportTicketEvent.EventType.ATTACHMENT_ADDED,
            actor=actor,
            new=attachment.original_filename,
        )

    if internal:
        events.record(
            ticket, SupportTicketEvent.EventType.INTERNAL_NOTE, actor=actor, new=message.pk
        )
        return message

    now = timezone.now()
    ticket.last_agent_message_at = now
    if ticket.first_response_at is None:
        ticket.first_response_at = now
    ticket.save(update_fields=["last_agent_message_at", "first_response_at", "updated_at"])
    events.record(ticket, SupportTicketEvent.EventType.AGENT_MESSAGE, actor=actor, new=message.pk)

    if ticket.status in AGENT_REPLY_PARKS:
        transition_ticket(
            ticket,
            to_status=SupportTicket.Status.WAITING_FOR_CUSTOMER,
            actor=actor,
            request=request,
        )

    notifications.notify_customer(
        ticket,
        headline=_("The support team replied on ticket %(number)s") % {"number": ticket.number},
        request=request,
        actor=actor,
    )
    return message
