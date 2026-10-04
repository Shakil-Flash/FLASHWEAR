"""FLASH Loop transitions: the controlled state machine (Phase 13).

Every status change in the loop goes through a function here. Nothing
in views, serializers or admin ever writes ``status = ...`` directly:
the graph (:attr:`LoopItem.ALLOWED_TRANSITIONS`) plus the per-type
gating below is what makes an illegal jump impossible, no matter
which surface tries to make it.

Three rules that hold for every function in this module:

* **Idempotent where it matters** -- ``submit``, ``approve``,
  ``complete_trade_in``, ``complete_recycling`` and ``publish``
  detect "already there" and return silently, so a retried request
  cannot double-submit, double-approve or double-award. Explicit
  references (unique constraints, ``trade-in:<id>`` credits) do the
  heavy lifting; timing never does.
* **Staff-gated moderation** -- functions only staff may call
  (:func:`start_review`, :func:`approve_resale`, :func:`reject_item`,
  :func:`set_authenticity`, ...) check ``actor.is_staff`` themselves,
  so a future view cannot forget to.
* **Transactional** -- a loop item, its listing and its trade-in /
  recycle request move together, or none of them move.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.loop.models import LoopCredit, LoopItem, RecycleRequest, ResaleListing, TradeInRequest
from apps.loop.services.eligibility import verify_loop_eligibility
from apps.loop.services.errors import LoopError, OwnershipError, TransitionError
from apps.loop.services.valuation import estimate_trade_in_credit

__all__ = [
    "accept_recycling",
    "accept_trade_in",
    "approve_resale",
    "cancel_loop_item",
    "complete_recycling",
    "complete_sale",
    "complete_trade_in",
    "create_loop_item",
    "decline_trade_in",
    "offer_trade_in",
    "publish_listing",
    "reject_item",
    "reject_recycling",
    "reserve_listing",
    "set_authenticity",
    "start_review",
    "submit_loop_item",
]


def _require_staff(actor, action: str) -> None:
    """Moderation is staff-only, enforced here and not just at the view."""
    if actor is None or not getattr(actor, "is_staff", False):
        raise LoopError(
            "Only FLASHWEAR moderators can do that.",
            code="loop_not_staff",
        )


def _require_owner(item: LoopItem, actor) -> None:
    """The acting user must own the item."""
    if actor is None or item.user_id != actor.id:
        raise OwnershipError("That loop item is not yours.", code="loop_not_owner")


def _transition(item: LoopItem, target: str, *, actor=None, note: str = "") -> LoopItem:
    """Move ``item`` to ``target`` if the graph allows it.

    Repeating a transition (``target == item.status``) is a no-op on
    purpose: retries and double-clicks must not raise.
    """
    if target == item.status:
        return item
    if not item.can_transition_to(target):
        raise TransitionError(
            f"A {item.get_type_display().lower()} item cannot go from "
            f"{item.get_status_display().lower()} to "
            f"{LoopItem.Status(target).label.lower()}.",
            code="loop_bad_transition",
        )
    item.status = target
    update_fields = {"status", "updated_at"}

    if target == LoopItem.Status.LISTED and item.listed_at is None:
        item.listed_at = timezone.now()
        update_fields.add("listed_at")
    if target == LoopItem.Status.SOLD and item.sold_at is None:
        item.sold_at = timezone.now()
        update_fields.add("sold_at")
    if target in (
        LoopItem.Status.UNDER_REVIEW,
        LoopItem.Status.APPROVED,
        LoopItem.Status.REJECTED,
    ):
        if item.reviewed_at is None:
            item.reviewed_at = timezone.now()
        update_fields.add("reviewed_at")
        if actor is not None:
            item.reviewed_by = actor
            update_fields.add("reviewed_by")
    if note:
        item.moderation_note = note[:300]
        update_fields.add("moderation_note")

    item.save(update_fields=sorted(update_fields))
    return item


# =============================================================================
# Creation
# =============================================================================


@transaction.atomic
def create_loop_item(
    user,
    loop_type: str,
    *,
    condition: str,
    closet_item_id: int | None = None,
    order_item_id: int | None = None,
    variant_id: int | None = None,
    product_id: int | None = None,
    title: str = "",
    description: str = "",
    condition_notes: str = "",
    asking_price: Decimal | None = None,
) -> LoopItem:
    """Verify ownership, then create a ``DRAFT`` loop item.

    The only entry point that turns a browser payload into a row.
    Everything a client can set is named here explicitly: ``status``,
    ``authenticity_status``, ``user`` and the evidence columns are
    all derived server-side.
    """
    evidence = verify_loop_eligibility(
        user,
        loop_type,
        closet_item_id=closet_item_id,
        order_item_id=order_item_id,
        variant_id=variant_id,
        product_id=product_id,
    )

    if loop_type == LoopItem.Type.RESALE and asking_price is None:
        raise LoopError("A resale item needs an asking price.", code="loop_missing_price")
    if loop_type != LoopItem.Type.RESALE:
        asking_price = None

    if condition not in dict(LoopItem.Condition.choices):
        raise LoopError("Unknown condition.", code="loop_bad_condition")

    item = LoopItem(
        user=user,
        type=loop_type,
        condition=condition,
        condition_notes=(condition_notes or "").strip()[:2000],
        title=(title or "").strip()[:200],
        description=(description or "").strip()[:5000],
        asking_price=asking_price,
        product=evidence.product,
        variant=evidence.variant,
        order_item=evidence.order_item,
        closet_item=evidence.closet_item,
        status=LoopItem.Status.DRAFT,
    )
    item.full_clean()
    item.save()
    return item


def submit_loop_item(item: LoopItem, *, actor=None) -> LoopItem:
    """DRAFT -> SUBMITTED, creating the child request/listing row.

    Idempotent: resubmitting an already-submitted item returns it.
    """
    actor = actor or item.user
    _require_owner(item, actor)
    if item.status == LoopItem.Status.SUBMITTED:
        return item
    if item.status != LoopItem.Status.DRAFT:
        raise TransitionError("Only a draft can be submitted.", code="loop_not_draft")
    with transaction.atomic():
        if item.type == LoopItem.Type.RESALE:
            _ensure_listing(item)
        elif item.type == LoopItem.Type.TRADE_IN:
            _ensure_trade_in(item)
        elif item.type == LoopItem.Type.RECYCLE:
            _ensure_recycle(item)
        return _transition(item, LoopItem.Status.SUBMITTED, actor=actor)


# -- child rows ---------------------------------------------------------------


def _ensure_listing(item: LoopItem) -> ResaleListing:
    """Create the pending-review listing once, at submit time."""
    if item.pk and hasattr(item, "resale_listing"):
        return item.resale_listing
    listing = ResaleListing(
        loop_item=item,
        seller=item.user,
        asking_price=item.asking_price,
        status=ResaleListing.Status.PENDING_REVIEW,
    )
    listing.save()
    return listing


def _ensure_trade_in(item: LoopItem) -> TradeInRequest:
    if item.pk and hasattr(item, "trade_in_request"):
        return item.trade_in_request
    request = TradeInRequest(
        loop_item=item,
        user=item.user,
        status=TradeInRequest.Status.SUBMITTED,
        estimated_credit=estimate_trade_in_credit(item),
    )
    request.save()
    return request


def _ensure_recycle(item: LoopItem) -> RecycleRequest:
    if item.pk and hasattr(item, "recycle_request"):
        return item.recycle_request
    request = RecycleRequest(
        loop_item=item, user=item.user, status=RecycleRequest.Status.SUBMITTED
    )
    request.save()
    return request


# =============================================================================
# Moderation (staff only)
# =============================================================================


@transaction.atomic
def start_review(item: LoopItem, *, actor) -> LoopItem:
    """SUBMITTED -> UNDER_REVIEW. Staff only. Idempotent."""
    _require_staff(actor, "start review")
    if item.status == LoopItem.Status.UNDER_REVIEW:
        return item
    if item.status != LoopItem.Status.SUBMITTED:
        raise TransitionError(
            "Only a submitted item can be reviewed.", code="loop_not_submitted"
        )
    if item.type == LoopItem.Type.TRADE_IN:
        request = _trade_in(item)
        if request.status == TradeInRequest.Status.SUBMITTED:
            request.status = TradeInRequest.Status.UNDER_REVIEW
            request.save(update_fields=["status", "updated_at"])
    if item.type == LoopItem.Type.RECYCLE:
        request = _recycle(item)
        if request.status == RecycleRequest.Status.SUBMITTED:
            # The recycle request graph has no separate review node; the loop
            # item's UNDER_REVIEW is the moderation gate.
            pass
    return _transition(item, LoopItem.Status.UNDER_REVIEW, actor=actor)


@transaction.atomic
def approve_resale(item: LoopItem, *, actor, note: str = "") -> LoopItem:
    """UNDER_REVIEW -> APPROVED for a resale item. Staff only. Idempotent."""
    _require_staff(actor, "approve")
    if item.type != LoopItem.Type.RESALE:
        raise LoopError("Only resale items can be approved for listing.", code="loop_not_resale")
    if item.status == LoopItem.Status.APPROVED:
        return item
    _ensure_listing(item)
    return _transition(item, LoopItem.Status.APPROVED, actor=actor, note=note)


@transaction.atomic
def reject_item(item: LoopItem, *, actor, note: str = "") -> LoopItem:
    """UNDER_REVIEW -> REJECTED. Staff only. Idempotent.

    A draft or submitted item must be reviewed first, so every
    rejection passes through a human (or an explicit review step).
    """
    _require_staff(actor, "reject")
    if item.status == LoopItem.Status.REJECTED:
        return item
    if item.status == LoopItem.Status.SUBMITTED:
        _transition(item, LoopItem.Status.UNDER_REVIEW, actor=actor)
    if item.type == LoopItem.Type.RESALE and item.pk and hasattr(item, "resale_listing"):
        listing = item.resale_listing
        if listing.status not in (
            ResaleListing.Status.SOLD,
            ResaleListing.Status.CANCELLED,
        ):
            listing.status = ResaleListing.Status.CANCELLED
            listing.save(update_fields=["status", "updated_at"])
    if item.type == LoopItem.Type.TRADE_IN and item.pk and hasattr(item, "trade_in_request"):
        request = item.trade_in_request
        if request.can_transition_to(TradeInRequest.Status.DECLINED):
            request.status = TradeInRequest.Status.DECLINED
            request.save(update_fields=["status", "updated_at"])
    if item.type == LoopItem.Type.RECYCLE and item.pk and hasattr(item, "recycle_request"):
        request = item.recycle_request
        if request.can_transition_to(RecycleRequest.Status.REJECTED):
            request.status = RecycleRequest.Status.REJECTED
            request.save(update_fields=["status", "updated_at"])
    return _transition(item, LoopItem.Status.REJECTED, actor=actor, note=note)


@transaction.atomic
def set_authenticity(item: LoopItem, *, actor, status: str) -> LoopItem:
    """Change the authenticity state. Staff only; sellers never can."""
    _require_staff(actor, "verify authenticity")
    if status not in dict(LoopItem.AuthenticityStatus.choices):
        raise LoopError("Unknown authenticity state.", code="loop_bad_authenticity")
    if item.authenticity_status != status:
        item.authenticity_status = status
        item.save(update_fields=["authenticity_status", "updated_at"])
    return item


# =============================================================================
# Resale lifecycle
# =============================================================================


@transaction.atomic
def publish_listing(item: LoopItem, *, actor) -> ResaleListing:
    """APPROVED -> LISTED and PENDING_REVIEW -> ACTIVE. Staff only.

    Idempotent: a listing already active is returned untouched.
    """
    _require_staff(actor, "publish")
    listing = _ensure_listing(item)
    if item.status == LoopItem.Status.LISTED:
        return listing
    _transition(item, LoopItem.Status.LISTED, actor=actor)
    if listing.status != ResaleListing.Status.ACTIVE:
        listing.status = ResaleListing.Status.ACTIVE
        listing.stamp("listed_at")
        listing.save(update_fields=["status", "listed_at", "updated_at"])
    return listing


@transaction.atomic
def reserve_listing(item: LoopItem, *, actor=None) -> ResaleListing:
    """LISTED -> RESERVED. Exactly one reservation can win: the graph
    makes a second ``reserve`` a no-op rather than a double-claim."""
    listing = _ensure_listing(item)
    if item.status == LoopItem.Status.RESERVED:
        return listing
    _transition(item, LoopItem.Status.RESERVED, actor=actor)
    listing.status = ResaleListing.Status.RESERVED
    listing.save(update_fields=["status", "updated_at"])
    return listing


@transaction.atomic
def complete_sale(item: LoopItem, *, actor=None) -> ResaleListing:
    """LISTED/RESERVED -> SOLD. Idempotent."""
    listing = _ensure_listing(item)
    if item.status == LoopItem.Status.SOLD:
        return listing
    _transition(item, LoopItem.Status.SOLD, actor=actor)
    listing.status = ResaleListing.Status.SOLD
    listing.stamp("sold_at")
    listing.save(update_fields=["status", "sold_at", "updated_at"])
    return listing


@transaction.atomic
def cancel_loop_item(item: LoopItem, *, actor, note: str = "") -> LoopItem:
    """Any non-terminal state -> CANCELLED.

    The owner may cancel their own item; staff may cancel any. Both
    child rows (listing / trade-in / recycle request) move with it.
    Idempotent.
    """
    if actor is None:
        raise LoopError("An actor is required to cancel.", code="loop_no_actor")
    if not getattr(actor, "is_staff", False):
        _require_owner(item, actor)
    if item.status == LoopItem.Status.CANCELLED:
        return item
    if item.type == LoopItem.Type.RESALE and item.pk and hasattr(item, "resale_listing"):
        listing = item.resale_listing
        if listing.status not in (
            ResaleListing.Status.SOLD,
            ResaleListing.Status.CANCELLED,
        ):
            listing.status = ResaleListing.Status.CANCELLED
            listing.save(update_fields=["status", "updated_at"])
    if item.type == LoopItem.Type.TRADE_IN and item.pk and hasattr(item, "trade_in_request"):
        request = item.trade_in_request
        if request.can_transition_to(TradeInRequest.Status.CANCELLED):
            request.status = TradeInRequest.Status.CANCELLED
            request.save(update_fields=["status", "updated_at"])
    if item.type == LoopItem.Type.RECYCLE and item.pk and hasattr(item, "recycle_request"):
        request = item.recycle_request
        if request.can_transition_to(RecycleRequest.Status.CANCELLED):
            request.status = RecycleRequest.Status.CANCELLED
            request.save(update_fields=["status", "updated_at"])
    return _transition(item, LoopItem.Status.CANCELLED, actor=actor, note=note)


# =============================================================================
# Trade-in lifecycle
# =============================================================================


def _trade_in(item: LoopItem) -> TradeInRequest:
    if not item.pk or not hasattr(item, "trade_in_request"):
        raise LoopError("This item is not a trade-in.", code="loop_not_trade_in")
    return item.trade_in_request


@transaction.atomic
def offer_trade_in(
    item: LoopItem,
    *,
    actor,
    final_credit: Decimal | None = None,
    note: str = "",
) -> TradeInRequest:
    """UNDER_REVIEW -> OFFERED, recording the reviewer's number.

    ``estimated_credit`` stays as the policy output; ``final_credit``
    is the reviewer's (different!) number. Staff only. Idempotent.
    """
    _require_staff(actor, "offer trade-in")
    request = _trade_in(item)
    if request.status == TradeInRequest.Status.OFFERED:
        return request
    if not request.can_transition_to(TradeInRequest.Status.OFFERED):
        raise TransitionError(
            "A trade-in offer cannot be made from this state.",
            code="loop_bad_transition",
        )
    if final_credit is not None:
        if final_credit < 0:
            raise LoopError("The credit cannot be negative.", code="loop_bad_credit")
        request.final_credit = final_credit
    if request.estimated_credit is None:
        request.estimated_credit = estimate_trade_in_credit(item)
    request.reviewer = actor
    request.reviewed_at = timezone.now()
    request.status = TradeInRequest.Status.OFFERED
    if note:
        request.reviewer_note = note[:300]
    request.save()
    return request


@transaction.atomic
def accept_trade_in(item: LoopItem, *, actor) -> TradeInRequest:
    """OFFERED -> ACCEPTED by the **owner**. Idempotent."""
    _require_owner(item, actor)
    request = _trade_in(item)
    if request.status == TradeInRequest.Status.ACCEPTED:
        return request
    if not request.can_transition_to(TradeInRequest.Status.ACCEPTED):
        raise TransitionError(
            "This trade-in offer can no longer be accepted.",
            code="loop_bad_transition",
        )
    if request.final_credit is None and request.estimated_credit is None:
        raise LoopError("There is no credit offer to accept.", code="loop_no_offer")
    request.status = TradeInRequest.Status.ACCEPTED
    request.save(update_fields=["status", "updated_at"])
    if item.status == LoopItem.Status.UNDER_REVIEW:
        _transition(item, LoopItem.Status.TRADE_IN_ACCEPTED, actor=actor)
    return request


@transaction.atomic
def decline_trade_in(item: LoopItem, *, actor) -> TradeInRequest:
    """OFFERED -> DECLINED by the owner; the loop item is cancelled.

    Idempotent.
    """
    _require_owner(item, actor)
    request = _trade_in(item)
    if request.status == TradeInRequest.Status.DECLINED:
        return request
    if not request.can_transition_to(TradeInRequest.Status.DECLINED):
        raise TransitionError(
            "This trade-in cannot be declined now.", code="loop_bad_transition"
        )
    request.status = TradeInRequest.Status.DECLINED
    request.save(update_fields=["status", "updated_at"])
    if item.is_active:
        _transition(item, LoopItem.Status.CANCELLED, actor=actor)
    return request


@transaction.atomic
def complete_trade_in(item: LoopItem, *, actor) -> LoopCredit:
    """TRADE_IN_ACCEPTED -> TRADE_IN_COMPLETED and award the credit.

    Staff only. The credit award is **idempotent**: the reference
    ``trade-in:<request_id>`` is unique, so a replayed completion
    fetches the existing credit instead of minting a second one.
    """
    _require_staff(actor, "complete trade-in")
    request = _trade_in(item)
    if item.status == LoopItem.Status.TRADE_IN_COMPLETED:
        existing = LoopCredit.objects.filter(reference=f"trade-in:{request.pk}").first()
        if existing is not None:
            return existing
    if not item.can_transition_to(LoopItem.Status.TRADE_IN_COMPLETED):
        raise TransitionError(
            "This trade-in cannot be completed yet.", code="loop_bad_transition"
        )
    credit_amount = request.final_credit if request.final_credit is not None else request.estimated_credit
    if credit_amount is None:
        raise LoopError("This trade-in has no credit to award.", code="loop_no_offer")

    _transition(item, LoopItem.Status.TRADE_IN_COMPLETED, actor=actor)
    request.status = TradeInRequest.Status.COMPLETED
    request.completed_at = timezone.now()
    request.save(update_fields=["status", "completed_at", "updated_at"])
    credit, _created = LoopCredit.objects.get_or_create(
        reference=f"trade-in:{request.pk}",
        defaults={
            "user": request.user,
            "amount": credit_amount,
            "loop_item": item,
            "trade_in_request": request,
        },
    )
    return credit


# =============================================================================
# Recycling lifecycle
# =============================================================================


def _recycle(item: LoopItem) -> RecycleRequest:
    if not item.pk or not hasattr(item, "recycle_request"):
        raise LoopError("This item is not a recycling request.", code="loop_not_recycle")
    return item.recycle_request


@transaction.atomic
def accept_recycling(item: LoopItem, *, actor) -> RecycleRequest:
    """SUBMITTED/UNDER_REVIEW -> RECYCLE_ACCEPTED. Staff only.

    The loop item passes through UNDER_REVIEW first (the moderation
    gate), then lands on RECYCLE_ACCEPTED. Idempotent.
    """
    _require_staff(actor, "accept recycling")
    request = _recycle(item)
    if item.status == LoopItem.Status.RECYCLE_ACCEPTED:
        return request
    if item.status == LoopItem.Status.SUBMITTED:
        _transition(item, LoopItem.Status.UNDER_REVIEW, actor=actor)
    if not item.can_transition_to(LoopItem.Status.RECYCLE_ACCEPTED):
        raise TransitionError(
            "This recycling request cannot be accepted now.", code="loop_bad_transition"
        )
    if not request.can_transition_to(RecycleRequest.Status.ACCEPTED):
        raise TransitionError(
            "This recycling request cannot be accepted now.", code="loop_bad_transition"
        )
    request.status = RecycleRequest.Status.ACCEPTED
    request.save(update_fields=["status", "updated_at"])
    _transition(item, LoopItem.Status.RECYCLE_ACCEPTED, actor=actor)
    return request


@transaction.atomic
def reject_recycling(item: LoopItem, *, actor, note: str = "") -> RecycleRequest:
    """SUBMITTED/UNDER_REVIEW -> REJECTED. Staff only. Idempotent."""
    _require_staff(actor, "reject recycling")
    request = _recycle(item)
    if request.status == RecycleRequest.Status.REJECTED:
        return request
    if item.status == LoopItem.Status.SUBMITTED:
        _transition(item, LoopItem.Status.UNDER_REVIEW, actor=actor)
    if not request.can_transition_to(RecycleRequest.Status.REJECTED):
        raise TransitionError(
            "This recycling request cannot be rejected now.", code="loop_bad_transition"
        )
    request.status = RecycleRequest.Status.REJECTED
    request.save(update_fields=["status", "updated_at"])
    if item.is_active:
        _transition(item, LoopItem.Status.REJECTED, actor=actor, note=note)
    return request


@transaction.atomic
def complete_recycling(item: LoopItem, *, actor) -> RecycleRequest:
    """RECYCLE_ACCEPTED -> RECYCLE_COMPLETED, advancing the request
    through RECEIVED and PROCESSED on the way. Staff only. Idempotent."""
    _require_staff(actor, "complete recycling")
    request = _recycle(item)
    if item.status == LoopItem.Status.RECYCLE_COMPLETED:
        return request
    if not item.can_transition_to(LoopItem.Status.RECYCLE_COMPLETED):
        raise TransitionError(
            "This recycling request cannot be completed yet.",
            code="loop_bad_transition",
        )
    if request.status == RecycleRequest.Status.SUBMITTED:
        request.status = RecycleRequest.Status.ACCEPTED
    if request.status == RecycleRequest.Status.ACCEPTED:
        request.status = RecycleRequest.Status.RECEIVED
        request.received_at = timezone.now()
    if request.status == RecycleRequest.Status.RECEIVED:
        request.status = RecycleRequest.Status.PROCESSED
        request.processed_at = timezone.now()
    request.save()
    _transition(item, LoopItem.Status.RECYCLE_COMPLETED, actor=actor)
    return request
