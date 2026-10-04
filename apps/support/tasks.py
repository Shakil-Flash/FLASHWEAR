"""FLASH Support Celery tasks (Phase 15).

Housekeeping only -- **no correctness depends on a task running**. A worker that is down
delays a reminder, never corrupts a transcript:

* :func:`close_abandoned_tickets` moves tickets the customer answered and the desk never
  came back to out of the live queue, so "waiting for us" cannot rot there forever;
* :func:`remind_pending_tickets` re-sends the "we need one detail" nudge for tickets that
  have been waiting on the customer longer than the configured window.

Neither task writes a status the graph would not allow: both go through
:func:`apps.support.services.tickets.transition_ticket`, so an overdue sweep still records
its own audit row and still notifies like any other transition.
"""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.support.models import SupportTicket
from apps.support.services.tickets import transition_ticket

__all__ = ["close_abandoned_tickets", "remind_pending_tickets"]


@shared_task(name="support.close_abandoned_tickets")
def close_abandoned_tickets() -> int:
    """Close RESOLVED tickets nobody reopened within ``SUPPORT_CLOSE_AFTER_DAYS``."""
    days = getattr(settings, "SUPPORT_CLOSE_AFTER_DAYS", 14)
    cutoff = timezone.now() - timedelta(days=days)
    stale = list(
        SupportTicket.objects.filter(
            status=SupportTicket.Status.RESOLVED,
            resolved_at__isnull=False,
            resolved_at__lt=cutoff,
        ).values_list("pk", flat=True)[:500]
    )
    closed = 0
    for pk in stale:
        ticket = SupportTicket.objects.filter(pk=pk).first()
        if ticket is None:
            continue
        try:
            transition_ticket(
                ticket, to_status=SupportTicket.Status.CLOSED, actor=None, notify=False
            )
        except Exception:  # one bad row must not stop the sweep
            continue
        closed += 1
    return closed


@shared_task(name="support.remind_pending_tickets")
def remind_pending_tickets() -> int:
    """Count tickets parked on the customer for longer than the reminder window.

    Returns the number of overdue tickets. This phase deliberately does **not** email from
    the task: sending from a worker without a request context needs the absolute base URL
    to be configured, and an unconfigured deployment sending guessed links is worse than
    not sending at all. The count is what the desk acts on.
    """
    from apps.support.services.notifications import notify_customer  # local: import cycle

    days = getattr(settings, "SUPPORT_REMIND_AFTER_DAYS", 3)
    cutoff = timezone.now() - timedelta(days=days)
    overdue = SupportTicket.objects.filter(
        status=SupportTicket.Status.WAITING_FOR_CUSTOMER,
        last_agent_message_at__isnull=False,
        last_agent_message_at__lt=cutoff,
    )
    count = overdue.count()
    if not count:
        return 0
    if not getattr(settings, "SUPPORT_SEND_REMINDERS", False):
        return count
    sent = 0
    for ticket in overdue.select_related("customer")[:100]:
        if notify_customer(
            ticket,
            headline=f"We are still waiting on ticket {ticket.number}",
        ):
            sent += 1
    return sent
