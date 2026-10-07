"""The operational audit trail (Phase 16).

One function, :func:`record`, and one rule: **write it in the same breath as the action**.
An audit row written afterwards, or in a separate process, is a row that can be forgotten;
``record()`` is called inside the view right after the domain service returns, so a
failure before the write leaves no claim of success, and a failure *after* it is visible
as a state change without a log line (the one gap worth reporting, not worth hiding).

What is never written: secrets, credentials, card data, session tokens, attachment bytes,
or a request payload. Metadata is a small dict of identifiers and counts chosen here.
"""

from __future__ import annotations

from apps.backoffice.models import AuditEvent

__all__ = ["ACTIONS", "record"]

#: Every action string the back office writes. Kept here (not in the views) so the audit
#: filter menu, the tests and the writers cannot drift apart. Adding an action is a
#: one-line change in this tuple and nowhere else.
ACTIONS = (
    "inventory.adjust",
    "order.status",
    "order.note",
    "return.approve",
    "return.reject",
    "return.receive",
    "return.inspect",
    "return.refund",
    "return.exchange",
    "loyalty.adjust",
    "catalog.publish",
    "catalog.archive",
    "catalog.unpublish",
    "catalog.bulk",
    "catalog.import",
    "catalog.create",
    "catalog.update",
    "catalog.variant",
    "catalog.image",
    "review.moderate",
    "loop.moderate",
    "loop.authenticity",
    "support.assign",
    "support.status",
    "staff.role",
    "bulk.action",
    "content.section.create",
    "content.section.update",
    "content.section.delete",
    "content.section.toggle",
    "content.editorial.create",
    "content.editorial.update",
    "content.editorial.delete",
    "content.campaign.create",
    "content.campaign.update",
    "content.campaign.delete",
    "merch.product.add",
    "merch.product.remove",
    "merch.product.reorder",
)

_DOMAINS = {value for value, _label in AuditEvent.Domain.choices}


def record(
    *,
    actor,
    domain: str,
    action: str,
    object_type: str,
    object_id: str | int = "",
    object_repr: str = "",
    reason: str = "",
    metadata: dict | None = None,
) -> AuditEvent:
    """Append one audit row.

    Args:
        actor: the staff user (``None`` is allowed for system tasks, but every back
            office action has one).
        domain: an :class:`AuditEvent.Domain` value -- filtering by area later.
        action: one of :data:`ACTIONS`.
        object_type: stable type name, e.g. ``inventory.stock``.
        object_id: primary key or human handle, stored as text so it survives renames.
        object_repr: what a human would call the row (``str(obj)``).
        reason: the operator's stated reason, when the action asked for one.
        metadata: small JSON-serialisable context. Never secrets.

    Raises:
        ValueError: an unknown domain or action -- a typo must fail loudly in development
            rather than produce a log row nobody can filter on.
    """
    if domain not in _DOMAINS:
        raise ValueError(f"Unknown audit domain: {domain!r}")
    if action not in ACTIONS:
        raise ValueError(f"Unknown audit action: {action!r}")

    return AuditEvent.objects.create(
        actor=actor if actor is not None and actor.pk else None,
        actor_label=(getattr(actor, "email", "") or "system"),
        domain=domain,
        action=action,
        object_type=object_type,
        object_id=str(object_id or ""),
        object_repr=str(object_repr or "")[:255],
        reason=str(reason or "")[:255],
        metadata=metadata or {},
    )
