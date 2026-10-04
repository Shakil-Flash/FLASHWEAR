"""The only writes the Back Office performs (Phase 16).

Every function here follows the same shape:

1. call the **domain service** that owns the rule -- :func:`apps.inventory.services.adjust_stock`,
   :func:`apps.engagement.services.loyalty.adjust_balance`,
   :func:`apps.engagement.services.reviews.moderate`, ``apps.orders.services.*``,
   ``apps.loop.services.loop_items.*``, ``apps.support.services.tickets.*``;
2. let its own exception (``InsufficientStock``, ``LoyaltyError``, ``InvalidTransition``,
   ``LoopError``, ``SupportError``) explain the failure -- the back office never restates
   a rule it did not write;
3. append exactly one audit row describing who did what to which object.

Nothing sets a status, a counter or a balance directly. A form field may *choose* an
argument; it may never bypass the service that validates it.
"""

from __future__ import annotations

from typing import NamedTuple

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.backoffice.permissions import BACKOFFICE_GROUPS
from apps.backoffice.services import audit as audit_service
from apps.catalog import services as catalog_services
from apps.engagement.models import PointsTransaction, Review
from apps.engagement.services import loyalty as loyalty_service
from apps.engagement.services import reviews as review_service
from apps.engagement.services.errors import EngagementError
from apps.inventory import services as inventory_service
from apps.inventory.models import InventoryMovement
from apps.inventory.services import InsufficientStock
from apps.loop.services import loop_items as loop_service
from apps.loop.services.errors import LoopError
from apps.orders import services as order_service
from apps.orders.models import InvalidTransition, Order
from apps.support.models import SupportTicket
from apps.support.services import tickets as support_tickets
from apps.support.services.errors import SupportError

__all__ = [
    "DOMAIN_ERRORS",
    "BulkResult",
    "adjust_inventory",
    "adjust_points",
    "assign_support_ticket",
    "catalog_action",
    "error_message",
    "loop_action",
    "moderate_reviews",
    "order_action",
    "set_staff_groups",
    "set_support_status",
]

#: What a back-office screen may catch when it calls a service. Every one of these
#: carries a message meant for the operator; anything else is a bug and should 500.
DOMAIN_ERRORS = (
    ValidationError,
    ValueError,
    InvalidTransition,
    InsufficientStock,
    EngagementError,
    LoopError,
    SupportError,
)


def error_message(exc: Exception) -> str:
    """The operator-facing sentence for a domain failure."""
    message = getattr(exc, "message", None)
    return str(message) if message else str(exc)


class BulkResult(NamedTuple):
    """What a bulk action did: successes by id, failures with their reason."""

    succeeded: list
    failed: list[tuple[object, str]]

    @property
    def ok(self) -> bool:
        return not self.failed


# --------------------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------------------


@transaction.atomic
def adjust_inventory(
    *,
    variant,
    delta: int,
    kind: str,
    note: str,
    reason: str,
    actor,
) -> InventoryMovement | None:
    """One signed stock change, through the ledger-writing service, then one audit row.

    The service writes the movement; the newest row for that variant is returned so the
    view can show what moved. Negative stock is refused *there*, not here -- one place
    owns that rule.
    """
    inventory_service.adjust_stock(
        variant,
        delta,
        kind=kind,
        user=actor,
        note=note,
        reference="backoffice",
    )
    movement = (
        InventoryMovement.objects.filter(variant=variant).order_by("-created_at", "-pk").first()
    )
    audit_service.record(
        actor=actor,
        domain="inventory",
        action="inventory.adjust",
        object_type="inventory.stock",
        object_id=variant.pk,
        object_repr=variant.sku,
        reason=reason,
        metadata={"delta": int(delta), "kind": kind, "note": note},
    )
    return movement


# --------------------------------------------------------------------------------------
# Loyalty
# --------------------------------------------------------------------------------------


@transaction.atomic
def adjust_points(*, user, amount: int, reason: str, actor) -> PointsTransaction:
    """A manual FLASH Points adjustment: the ledger entry, then the audit row.

    The balance is never touched directly -- it is the sum of the ledger, so an
    adjustment that is not a ledger row does not exist.
    """
    transaction_row = loyalty_service.adjust_balance(user, int(amount), note=reason)
    audit_service.record(
        actor=actor,
        domain="loyalty",
        action="loyalty.adjust",
        object_type="accounts.user",
        object_id=user.pk,
        object_repr=user.email,
        reason=reason,
        metadata={"amount": int(amount), "ledger_reference": transaction_row.reference},
    )
    return transaction_row


# --------------------------------------------------------------------------------------
# Moderation
# --------------------------------------------------------------------------------------


@transaction.atomic
def moderate_reviews(*, reviews, status: str, reason: str, actor) -> BulkResult:
    """Move a set of reviews to ``status`` through the engagement service.

    ``moderate`` excludes rows already in that state, so ``changed`` is the number that
    actually moved while ``succeeded`` is what was asked for.
    """
    if status not in Review.Status.values:
        raise ValueError(f"Unknown review status: {status!r}")
    rows = list(reviews)
    changed = review_service.moderate(Review.objects.filter(pk__in=[r.pk for r in rows]), status)
    audit_service.record(
        actor=actor,
        domain="moderation",
        action="review.moderate",
        object_type="engagement.review",
        object_id=",".join(str(r.pk) for r in rows)[:64],
        object_repr=f"{len(rows)} review(s)",
        reason=reason,
        metadata={
            "status": status,
            "changed": changed,
            "reviews": [r.pk for r in rows],
        },
    )
    return BulkResult(succeeded=[r.pk for r in rows], failed=[])


@transaction.atomic
def loop_action(
    *,
    item,
    action: str,
    actor,
    note: str = "",
    reason: str = "",
    authenticity: str = "",
):
    """One FLASH Loop moderation decision, through the loop service.

    The service owns the graph and the ownership rules; the back office only chooses
    *which* decision and records *who* made it.
    """
    if action == "start_review":
        result = loop_service.start_review(item, actor=actor)
    elif action == "approve":
        result = loop_service.approve_resale(item, actor=actor, note=note)
    elif action == "reject":
        result = loop_service.reject_item(item, actor=actor, note=note)
    elif action == "publish":
        result = loop_service.publish_listing(item, actor=actor)
    elif action == "authenticity":
        result = loop_service.set_authenticity(item, actor=actor, status=authenticity)
    elif action == "accept_recycling":
        result = loop_service.accept_recycling(item, actor=actor)
    elif action == "reject_recycling":
        result = loop_service.reject_recycling(item, actor=actor, note=note)
    elif action == "decline_trade_in":
        result = loop_service.decline_trade_in(item, actor=actor)
    else:
        raise ValueError(f"Unknown loop action: {action!r}")

    audit_service.record(
        actor=actor,
        domain="loop",
        action="loop.authenticity" if action == "authenticity" else "loop.moderate",
        object_type="loop.loopitem",
        object_id=item.pk,
        object_repr=str(item),
        reason=reason or note,
        metadata={"decision": action, "status": getattr(result, "status", "")},
    )
    return result


# --------------------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------------------


def catalog_action(*, products, action: str, actor, reason: str = "") -> BulkResult:
    """Publish / archive / unpublish a selection, item by item.

    Each row is validated on its own (``publish`` runs ``full_clean``), so one product
    parked in an inactive category fails *that row* while the rest still move -- a partial
    success that is reported, never a silent half-applied bulk edit.
    """
    services = {
        "publish": catalog_services.publish,
        "archive": catalog_services.archive,
        "unpublish": catalog_services.unpublish,
    }
    if action not in services:
        raise ValueError(f"Unknown catalogue action: {action!r}")

    result = BulkResult(succeeded=[], failed=[])
    for product in products:
        try:
            services[action](product)
        except (ValidationError, ValueError) as exc:
            result.failed.append((product, error_message(exc)))
        else:
            result.succeeded.append(product.pk)
    if result.succeeded:
        audit_service.record(
            actor=actor,
            domain="catalog",
            action=f"catalog.{action}",
            object_type="catalog.product",
            object_id=",".join(str(pk) for pk in result.succeeded)[:64],
            object_repr=f"{len(result.succeeded)} product(s)",
            reason=reason,
            metadata={
                "action": action,
                "products": result.succeeded,
                "failed": [p.pk for p, _ in result.failed],
            },
        )
    return result


# --------------------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------------------

ORDER_ACTIONS = ("processing", "ship", "deliver", "cancel")


@transaction.atomic
def order_action(
    *,
    order: Order,
    action: str,
    actor,
    note: str = "",
    tracking_number: str = "",
    carrier: str = "",
) -> Order:
    """Advance or cancel an order through ``apps.orders.services``.

    The services own the transition graph and the stock release. Cancelling a *paid*
    order is a refund, and refunds are not this phase's business: that path lives in
    Django admin and the payments app, where it has its own rules and its own log.
    """
    if action not in ORDER_ACTIONS:
        raise ValueError(f"Unknown order action: {action!r}")

    before = order.status
    if action == "processing":
        order_service.mark_processing(order, actor=actor, carrier=carrier)
    elif action == "ship":
        order_service.ship_order(
            order, actor=actor, tracking_number=tracking_number, carrier=carrier
        )
    elif action == "deliver":
        order_service.deliver_order(order, actor=actor)
    else:
        order_service.cancel_order(order, actor=actor, note=note)
    order.refresh_from_db()

    audit_service.record(
        actor=actor,
        domain="orders",
        action="order.status",
        object_type="orders.order",
        object_id=order.number,
        object_repr=str(order),
        reason=note,
        metadata={"from": before, "to": order.status, "transition": action},
    )
    return order


# --------------------------------------------------------------------------------------
# Support
# --------------------------------------------------------------------------------------


@transaction.atomic
def assign_support_ticket(*, ticket, agent, actor) -> SupportTicket:
    """Assign (or release) a ticket through the support service.

    Whether the *actor* may do that is decided inside
    :func:`apps.support.services.tickets.assign_ticket` -- the desk's manager rule is not
    re-implemented here, so a back-office screen cannot grant what the desk would refuse.
    """
    before = ticket.assigned_to_id
    result = support_tickets.assign_ticket(ticket, actor=actor, agent=agent)
    audit_service.record(
        actor=actor,
        domain="support",
        action="support.assign",
        object_type="support.supportticket",
        object_id=ticket.number,
        object_repr=ticket.subject,
        metadata={"from": before, "to": getattr(agent, "email", None) or ""},
    )
    return result


@transaction.atomic
def set_support_status(*, ticket, status: str, actor) -> SupportTicket:
    """Move a ticket through the support state machine, then log who moved it."""
    result = support_tickets.transition_ticket(ticket, to_status=status, actor=actor)
    audit_service.record(
        actor=actor,
        domain="support",
        action="support.status",
        object_type="support.supportticket",
        object_id=ticket.number,
        object_repr=ticket.subject,
        metadata={"status": status},
    )
    return result


# --------------------------------------------------------------------------------------
# Staff & roles
# --------------------------------------------------------------------------------------


@transaction.atomic
def set_staff_groups(*, user, add: list[str], remove: list[str], actor) -> list[str]:
    """Grant and revoke back-office groups, then log both sides of the change.

    Only groups this app owns may be granted from here: the support desk's groups belong
    to the support app (and its own capability rules), and Django's model permissions
    belong to ``/admin/``. A privilege change is always audited -- even a removal.
    """
    unknown = (set(add) | set(remove)) - set(BACKOFFICE_GROUPS)
    if unknown:
        raise ValueError(f"Unknown group: {sorted(unknown)[0]!r}")

    current = set(user.groups.values_list("name", flat=True))
    added = [name for name in add if name not in current]
    removed = [name for name in remove if name in current]
    if added:
        user.groups.add(*Group.objects.filter(name__in=added))
    if removed:
        user.groups.remove(*Group.objects.filter(name__in=removed))

    if added or removed:
        audit_service.record(
            actor=actor,
            domain="staff",
            action="staff.role",
            object_type="accounts.user",
            object_id=user.pk,
            object_repr=user.email,
            metadata={"added": added, "removed": removed},
        )
    return added + removed
