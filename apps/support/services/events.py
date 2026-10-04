"""The ticket audit log: one helper, one row per thing that happened.

Every service that changes a ticket writes its event inside the same transaction as the
change, so history cannot drift from state (an "assigned" row without an assignee is a
schema bug, not a data race). Events are never updated or deleted -- see
:class:`apps.support.admin.SupportTicketEventAdmin`, which refuses all three.
"""

from __future__ import annotations

from apps.support.models import SupportTicket, SupportTicketEvent

__all__ = ["display_for", "record"]


def record(
    ticket: SupportTicket,
    event_type: str,
    *,
    actor=None,
    old: str = "",
    new: str = "",
    metadata: dict | None = None,
) -> SupportTicketEvent:
    """Append one audit row for ``ticket``.

    ``old`` / ``new`` are the values the change moved between ("open" -> "in_progress");
    they are stored as display strings so the history reads without a join.
    """
    return SupportTicketEvent.objects.create(
        ticket=ticket,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        event_type=event_type,
        old_value=str(old)[:200],
        new_value=str(new)[:200],
        metadata=metadata or {},
    )


def display_for(user) -> str:
    """A stable human label for an actor in history and notification copy."""
    if user is None or not getattr(user, "is_authenticated", False):
        return ""
    return user.get_full_name() or user.email
