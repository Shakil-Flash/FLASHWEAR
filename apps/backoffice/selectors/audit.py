"""Audit log reads (Phase 16).

The log is append-only, so every selector here is a read: there is no update path to
guard because :class:`apps.backoffice.models.AuditEvent` refuses one at the model layer.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.backoffice.models import AuditEvent
from apps.backoffice.selectors.common import DateRange, sort_queryset

__all__ = ["AUDIT_ACTIONS", "AUDIT_SORTS", "audit_events"]

AUDIT_SORTS = {
    "-created_at": ["-created_at", "-pk"],
    "created_at": ["created_at", "pk"],
    "action": ["action", "-created_at"],
    "actor": ["actor_label", "-created_at"],
}

#: Every action the back office writes. The filter menu is built from this, so the log
#: cannot offer an option nothing produces.
AUDIT_ACTIONS = (
    "inventory.adjust",
    "order.status",
    "order.note",
    "promotion.viewed",
    "loyalty.adjust",
    "catalog.publish",
    "catalog.archive",
    "review.moderate",
    "loop.moderate",
    "loop.authenticity",
    "support.assign",
    "support.status",
    "staff.role",
    "bulk.action",
)


def audit_events(
    *,
    q: str = "",
    actor: str = "",
    action: str = "",
    domain: str = "",
    object_type: str = "",
    date_range: DateRange | None = None,
    sort: str = "",
) -> QuerySet:
    """The audit log, filtered and ordered by allowlisted keys."""
    qs = AuditEvent.objects.select_related("actor")
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(
            Q(object_id__icontains=needle)
            | Q(object_repr__icontains=needle)
            | Q(actor_label__icontains=needle)
            | Q(reason__icontains=needle)
        )
    if actor:
        qs = qs.filter(actor_label__icontains=actor.strip())
    if action:
        qs = qs.filter(action=action)
    if domain and domain in AuditEvent.Domain.values:
        qs = qs.filter(domain=domain)
    if object_type:
        qs = qs.filter(object_type=object_type.strip())
    if date_range is not None and not date_range.is_all_time:
        qs = qs.filter(created_at__gte=date_range.start, created_at__lt=date_range.end)
    return sort_queryset(qs, sort, AUDIT_SORTS, "-created_at")[0]
