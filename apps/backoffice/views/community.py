"""Community screens: reviews, FLASH Loop, support, FLASH Points (Phase 16).

Four queues, four owners: moderation of a review belongs to engagement, a Loop decision to
the loop service, a ticket transition to the support desk, a points adjustment to the
loyalty ledger. This module *routes* decisions to those services and writes the audit row
that says who made them; it never decides anything itself.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.backoffice import selectors
from apps.backoffice.forms import (
    LoopActionForm,
    PointsAdjustForm,
    ReviewModerationForm,
    SupportAssignForm,
    SupportStatusForm,
)
from apps.backoffice.permissions import (
    LOYALTY_MANAGE,
    LOYALTY_VIEW,
    MODERATION_MANAGE,
    MODERATION_VIEW,
    SUPPORT_MANAGE,
    SUPPORT_VIEW,
    backoffice_access,
    has,
)
from apps.backoffice.services import operations
from apps.backoffice.views.base import range_from_request, render_bo
from apps.engagement.models import PointsTransaction, Review
from apps.loop.models import LoopItem
from apps.support.models import SupportTicket

__all__ = [
    "loop",
    "loop_action",
    "points",
    "points_adjust",
    "reviews",
    "reviews_bulk",
    "support",
    "support_assign",
    "support_status",
]

#: The four Loop queues, in the order the tabs are offered. ``?tab=`` may only name one
#: of these -- an unknown value falls back to the first.
LOOP_TABS = (
    ("items", "Items awaiting review"),
    ("listings", "Resale listings"),
    ("trade_ins", "Trade-ins"),
    ("recycling", "Recycling"),
)
LOOP_TABS_KEYS = {key for key, _label in LOOP_TABS}

#: Which item type each tab shows (``items`` and ``listings`` are not type-bound).
LOOP_TAB_TYPES = {"trade_ins": "trade_in", "recycling": "recycle"}


# --------------------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------------------


@backoffice_access(MODERATION_VIEW)
def reviews(request):
    """``/operations/reviews/`` -- the moderation queue, oldest pending first."""
    queryset = selectors.review_queue(
        q=request.GET.get("q", ""),
        status=request.GET.get("status", ""),
        rating=request.GET.get("rating", ""),
        sort=request.GET.get("sort", ""),
    )
    return render_bo(
        request,
        "backoffice/reviews.html",
        active="reviews",
        page=selectors.paginate(request, queryset),
        bulk_form=ReviewModerationForm(),
        filters={
            "q": request.GET.get("q", ""),
            "status": request.GET.get("status", ""),
            "rating": request.GET.get("rating", ""),
            "sort": request.GET.get("sort", ""),
        },
        status_choices=Review.Status.choices,
        can_manage=has(request.user, MODERATION_MANAGE),
    )


@require_POST
@backoffice_access(MODERATION_MANAGE)
def reviews_bulk(request):
    """Publish / reject / hide the selection through the engagement service."""
    form = ReviewModerationForm(request.POST)
    ids = request.POST.getlist("selected")
    if not form.is_valid() or not ids:
        messages.error(request, "Select at least one review and choose a decision.")
        return redirect("backoffice:reviews")

    try:
        result = operations.moderate_reviews(
            reviews=Review.objects.filter(pk__in=ids),
            status=form.cleaned_data["status"],
            reason=form.cleaned_data["reason"],
            actor=request.user,
        )
    except operations.DOMAIN_ERRORS as exc:
        messages.error(request, operations.error_message(exc))
    else:
        messages.success(
            request,
            f"{len(result.succeeded)} review(s) set to {form.cleaned_data['status']}.",
        )
    return redirect("backoffice:reviews")


# --------------------------------------------------------------------------------------
# FLASH Loop
# --------------------------------------------------------------------------------------


@backoffice_access(MODERATION_VIEW)
def loop(request):
    """``/operations/loop/`` -- submitted items plus the listing queue.

    The trade-in and recycling tabs are the *same* item table filtered to its own type,
    so every row on every tab offers the same decisions rather than a parallel screen
    with its own rules.
    """
    from apps.loop.models import LoopItem

    tab = request.GET.get("tab", "items")
    if tab not in LOOP_TABS_KEYS:
        tab = "items"
    requested_status = request.GET.get("status", "")
    type_filter = LOOP_TAB_TYPES.get(tab, "")
    # A type tab shows the whole history of that path unless the operator asked for a
    # state; the default tab shows the work that is waiting.
    default_status = "all" if type_filter else ""
    return render_bo(
        request,
        "backoffice/loop.html",
        active="loop",
        tab=tab,
        tabs=LOOP_TABS,
        status_choices=LoopItem.Status.choices,
        page=selectors.paginate(
            request,
            selectors.loop_items(
                status=requested_status or default_status,
                type_=type_filter,
                q=request.GET.get("q", ""),
            ),
        ),
        listings=selectors.resale_listings(status=request.GET.get("status", ""))[:100],
        counts=selectors.pending_loop_count(),
        action_form=LoopActionForm(),
        filters={
            "q": request.GET.get("q", ""),
            "status": requested_status,
            "type": type_filter,
        },
        can_manage=has(request.user, MODERATION_MANAGE),
    )


@require_POST
@backoffice_access(MODERATION_MANAGE)
def loop_action(request, pk: int):
    """One Loop decision, run by the loop service and logged here."""
    item = get_object_or_404(LoopItem, pk=pk)
    form = LoopActionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "That decision is not available.")
        return redirect("backoffice:loop")

    try:
        operations.loop_action(
            item=item,
            action=form.cleaned_data["action"],
            actor=request.user,
            note=form.cleaned_data["note"],
            reason=form.cleaned_data["reason"],
            authenticity=form.cleaned_data["authenticity"],
        )
    except operations.DOMAIN_ERRORS as exc:
        messages.error(request, operations.error_message(exc))
    else:
        messages.success(request, f"Loop item #{item.pk} updated.")
    return redirect("backoffice:loop")


# --------------------------------------------------------------------------------------
# Support
# --------------------------------------------------------------------------------------


@backoffice_access(SUPPORT_VIEW)
def support(request):
    """``/operations/support/`` -- queue metadata only; transcripts stay on the desk."""
    queryset = selectors.support_tickets(
        q=request.GET.get("q", ""),
        status=request.GET.get("status", ""),
        priority=request.GET.get("priority", ""),
        sort=request.GET.get("sort", ""),
    )
    return render_bo(
        request,
        "backoffice/support.html",
        active="support",
        page=selectors.paginate(request, queryset),
        assign_form=SupportAssignForm(),
        status_form=SupportStatusForm(),
        filters={
            "q": request.GET.get("q", ""),
            "status": request.GET.get("status", ""),
            "priority": request.GET.get("priority", ""),
            "sort": request.GET.get("sort", ""),
        },
        status_choices=SupportTicket.Status.choices,
        priority_choices=SupportTicket.Priority.choices,
        # The desk's own manager rule still applies to reassignment; the template hides
        # the control when this user could not complete it anyway.
        can_manage=has(request.user, SUPPORT_MANAGE),
    )


@require_POST
@backoffice_access(SUPPORT_MANAGE)
def support_assign(request, number: str):
    """Assign or release a ticket through ``apps.support.services.tickets``."""
    ticket = get_object_or_404(SupportTicket, number=number)
    form = SupportAssignForm(request.POST)
    if not form.is_valid():
        messages.error(request, "That assignment is not available.")
        return redirect("backoffice:support")

    try:
        operations.assign_support_ticket(
            ticket=ticket,
            agent=form.cleaned_data["agent"],
            actor=request.user,
        )
    except operations.DOMAIN_ERRORS as exc:
        messages.error(request, operations.error_message(exc))
    else:
        agent = form.cleaned_data["agent"]
        messages.success(
            request,
            f"Ticket {ticket.number} assigned to {agent.email if agent else 'nobody'}.",
        )
    return redirect("backoffice:support")


@require_POST
@backoffice_access(SUPPORT_MANAGE)
def support_status(request, number: str):
    """Move a ticket along its graph; an illegal edge is refused by the desk's service."""
    ticket = get_object_or_404(SupportTicket, number=number)
    form = SupportStatusForm(request.POST)
    if not form.is_valid():
        messages.error(request, "That status is not available.")
        return redirect("backoffice:support")

    try:
        operations.set_support_status(
            ticket=ticket,
            status=form.cleaned_data["status"],
            actor=request.user,
        )
    except operations.DOMAIN_ERRORS as exc:
        messages.error(request, operations.error_message(exc))
    else:
        messages.success(request, f"Ticket {ticket.number} is now {ticket.status}.")
    return redirect("backoffice:support")


# --------------------------------------------------------------------------------------
# FLASH Points
# --------------------------------------------------------------------------------------


@backoffice_access(LOYALTY_VIEW)
def points(request):
    """``/operations/points/`` -- the ledger and the manual-adjustment form."""
    date_range = range_from_request(request)
    queryset = selectors.points_ledger(
        q=request.GET.get("q", ""),
        transaction_type=request.GET.get("type", ""),
        date_range=date_range,
    )
    return render_bo(
        request,
        "backoffice/points.html",
        active="points",
        page=selectors.paginate(request, queryset),
        adjust_form=PointsAdjustForm(),
        current_range=date_range.key,
        type_choices=PointsTransaction.TransactionType.choices,
        filters={
            "q": request.GET.get("q", ""),
            "type": request.GET.get("type", ""),
        },
        can_manage=has(request.user, LOYALTY_MANAGE),
    )


@require_POST
@backoffice_access(LOYALTY_MANAGE)
def points_adjust(request):
    """Credit or debit FLASH Points through the loyalty ledger, then log it."""
    form = PointsAdjustForm(request.POST)
    if not form.is_valid():
        for field, errors in form.errors.items():
            messages.error(request, f"{field}: {' '.join(errors)}")
        return redirect("backoffice:points")

    data = form.cleaned_data
    try:
        operations.adjust_points(
            user=data["user"],
            amount=data["amount"],
            reason=data["reason"],
            actor=request.user,
        )
    except operations.DOMAIN_ERRORS as exc:
        messages.error(request, operations.error_message(exc))
    else:
        messages.success(request, f"{data['amount']:+d} points applied to {data['user'].email}.")
    return redirect("backoffice:points")
