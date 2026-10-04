"""Screens for FLASH Support (Phase 15).

Two audiences, two shells:

* the **customer** flow lives under ``/support/`` and extends the account area shell, so the
  section rail, the signed-in chrome and the ``noindex`` rule are shared with the rest of the
  private area;
* the **desk** lives under ``/support/staff/`` and renders on the plain storefront shell --
  it is an operator tool, not part of a customer's account.

Every screen is session-authenticated. Object access is scoped at the selector level, so
somebody else's ticket number 404s rather than 403s (existence is not disclosed), and the
staff routes 404 for anyone without the agent capability for the same reason: a help desk
that announces where it lives is a help desk that gets probed.

Error handling follows the project's split: domain failures surface as messages (HTML) or as
``{"detail", "code"}`` (API), and a rate limit re-renders the form with **429** rather than
silently accepting the write.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.support import selectors
from apps.support.forms import (
    MessageForm,
    StaffAssignForm,
    StaffEscalateForm,
    StaffLinkForm,
    StaffNoteForm,
    StaffPriorityForm,
    StaffStatusForm,
    TicketCreateForm,
)
from apps.support.models import SupportAttachment, SupportTicket
from apps.support.permissions import SUPPORT_AGENT, has_capability, staff_with_capability
from apps.support.services import messages as message_service
from apps.support.services import references
from apps.support.services import tickets as ticket_service
from apps.support.services.errors import SupportError

__all__ = [
    "attachment_download",
    "staff_assign",
    "staff_dashboard",
    "staff_escalate",
    "staff_link",
    "staff_note",
    "staff_priority",
    "staff_reply",
    "staff_status",
    "staff_ticket_detail",
    "support_home",
    "ticket_close",
    "ticket_create",
    "ticket_detail",
    "ticket_list",
    "ticket_reopen",
    "ticket_reply",
]

REFERENCE_KEYS = ("order", "product", "review", "loop_item", "quest")


def _require_agent(user) -> None:
    """Desk entry guard. 404, not 403: the URL space must not advertise where it is."""
    if not has_capability(user, SUPPORT_AGENT):
        raise Http404


def _post_error(request, exc: SupportError, number: str, *, staff: bool = False):
    """Translate a domain failure into a flash message and a redirect back."""
    messages.error(request, exc.message)
    name = "support:staff-ticket-detail" if staff else "support:ticket-detail"
    return redirect(name, number=number)


# =============================================================================
# Customer surface
# =============================================================================


@login_required
@never_cache
@require_GET
def support_home(request):
    """``/support/`` -- the front door: open tickets, and the way to start one."""
    open_tickets = selectors.user_tickets(request.user).filter(status__in=selectors.OPEN_STATUSES)
    context = {
        "open_tickets": list(open_tickets[:5]),
        "open_count": open_tickets.count(),
    }
    return render(request, "support/home.html", context)


@login_required
@never_cache
@require_GET
def ticket_list(request):
    """``/support/tickets/`` -- this customer's tickets, newest first."""
    status = request.GET.get("status", "")
    if status not in SupportTicket.Status.values:
        status = ""
    queryset = selectors.user_tickets(request.user, status=status)
    page = Paginator(queryset, settings.SUPPORT_TICKETS_PER_PAGE).get_page(request.GET.get("page"))
    context = {
        "page": page,
        "current_status": status,
        "status_choices": [("", "All"), *list(SupportTicket.Status.choices)],
    }
    return render(request, "support/ticket_list.html", context)


def _prefill(request) -> dict:
    """Handles from the query string, kept only if they resolve to *this* customer.

    The "Need help?" link carries an order number or a product slug so the customer never
    has to retype it. Re-resolving on the way in means a hand-edited URL cannot make the
    form echo back a record that does not belong to the person looking at it.
    """
    values = {
        key: request.GET.get(key, "").strip()
        for key in REFERENCE_KEYS
        if request.GET.get(key, "").strip()
    }
    if not values:
        return {}
    for key in ("review", "loop_item"):
        if key in values and not values[key].isdigit():
            values.pop(key)
    if not values:
        return {}
    try:
        references.resolve_for_customer(request.user, **values)
    except SupportError:
        # Nothing we can verify is shown: the customer gets a blank form instead of a
        # handle they are not entitled to.
        return {}
    return values


@login_required
@never_cache
def ticket_create(request):
    """``GET/POST /support/create/`` -- open a ticket.

    The only screen that can answer 429: opening tickets is the write an abuser reaches
    for first, so the limit re-renders the form with an error instead of quietly accepting
    the row.
    """
    if request.method == "POST":
        form = TicketCreateForm(request.POST)
        if form.is_valid():
            try:
                ticket = ticket_service.open_ticket(
                    customer=request.user,
                    subject=form.cleaned_data["subject"],
                    description=form.cleaned_data["description"],
                    category=form.cleaned_data["category"],
                    refs=form.references(),
                    uploads=request.FILES.getlist("files"),
                    request=request,
                )
            except SupportError as exc:
                if exc.code == "support_throttled":
                    messages.error(request, exc.message)
                    return render(
                        request,
                        "support/ticket_create.html",
                        {"form": form},
                        status=429,
                    )
                form.add_error(None, exc.message)
            else:
                messages.success(request, f"Ticket {ticket.number} opened.")
                return redirect("support:ticket-detail", number=ticket.number)
    else:
        form = TicketCreateForm(initial=_prefill(request))
    return render(request, "support/ticket_create.html", {"form": form})


def _detail_context(request, ticket, reply_form=None) -> dict:
    """Shared by the normal render and the 429 re-render, so they cannot drift."""
    can_respond = ticket.status != SupportTicket.Status.CLOSED
    return {
        "ticket": ticket,
        "transcript": selectors.user_messages(ticket),
        "milestones": _milestones(ticket),
        "reply_form": reply_form or MessageForm(),
        "can_respond": can_respond,
        "can_close": ticket.status not in (SupportTicket.Status.CLOSED,),
    }


def _milestones(ticket) -> list[dict]:
    """A customer-safe history: timestamps only, no actors and no internal events.

    The full audit trail includes agent emails and internal notes, so the customer gets
    the facts about their own ticket instead of a filtered view of the desk's log.
    """
    rows = [
        {"label": "Opened", "at": ticket.created_at},
    ]
    if ticket.first_response_at:
        rows.append({"label": "First reply from the team", "at": ticket.first_response_at})
    if ticket.assigned_at:
        rows.append({"label": "Picked up by the team", "at": ticket.assigned_at})
    if ticket.resolved_at:
        rows.append({"label": "Marked resolved", "at": ticket.resolved_at})
    if ticket.closed_at:
        rows.append({"label": "Closed", "at": ticket.closed_at})
    return rows


@login_required
@never_cache
@require_GET
def ticket_detail(request, number: str):
    """``/support/tickets/<number>/`` -- the transcript, for its owner only."""
    ticket = selectors.user_ticket(request.user, number)
    return render(request, "support/ticket_detail.html", _detail_context(request, ticket))


@login_required
@never_cache
@require_POST
def ticket_reply(request, number: str):
    """``POST /support/tickets/<number>/reply/`` -- the customer answers."""
    ticket = selectors.user_ticket(request.user, number)
    form = MessageForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Write a message first.")
        return redirect("support:ticket-detail", number=number)
    try:
        message_service.post_customer_message(
            ticket,
            customer=request.user,
            body=form.cleaned_data["body"],
            uploads=request.FILES.getlist("files"),
            request=request,
        )
    except SupportError as exc:
        messages.error(request, exc.message)
        if exc.code == "support_throttled":
            return render(
                request,
                "support/ticket_detail.html",
                _detail_context(request, ticket),
                status=429,
            )
        return redirect("support:ticket-detail", number=number)
    messages.success(request, "Reply sent.")
    return redirect("support:ticket-detail", number=number)


@login_required
@never_cache
@require_POST
def ticket_close(request, number: str):
    """``POST /support/tickets/<number>/close/`` -- the customer closes their own ticket."""
    ticket = selectors.user_ticket(request.user, number)
    try:
        ticket_service.close_ticket(ticket, actor=request.user, request=request)
    except SupportError as exc:
        return _post_error(request, exc, number)
    messages.success(request, "Ticket closed.")
    return redirect("support:ticket-detail", number=number)


@login_required
@never_cache
@require_POST
def ticket_reopen(request, number: str):
    """``POST /support/tickets/<number>/reopen/`` -- CLOSED -> IN_PROGRESS."""
    ticket = selectors.user_ticket(request.user, number)
    try:
        ticket_service.reopen_ticket(ticket, actor=request.user, request=request)
    except SupportError as exc:
        return _post_error(request, exc, number)
    messages.success(request, "Ticket reopened.")
    return redirect("support:ticket-detail", number=number)


@login_required
@never_cache
@require_GET
def attachment_download(request, pk: int):
    """``GET /support/attachments/<pk>/`` -- the only way out of private storage.

    Owner or agent, proved per request. Anyone else gets a 404: an attachment id that
    answers 403 would confirm that it exists and is worth guessing.
    """
    attachment = get_object_or_404(
        SupportAttachment.objects.select_related("message__ticket"), pk=pk
    )
    ticket = attachment.message.ticket
    allowed = ticket.customer_id == request.user.pk or has_capability(request.user, SUPPORT_AGENT)
    if not allowed:
        raise Http404
    try:
        handle = attachment.file.open("rb")
    except OSError:
        raise Http404 from None
    response = FileResponse(
        handle,
        content_type=attachment.content_type,
        as_attachment=True,
        filename=attachment.original_filename,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


# =============================================================================
# Desk surface
# =============================================================================


@login_required
@never_cache
@require_GET
def staff_dashboard(request):
    """``/support/staff/`` -- the queue, filtered and counted."""
    _require_agent(request.user)
    filters = {
        "status": request.GET.get("status", ""),
        "category": request.GET.get("category", ""),
        "q": (request.GET.get("q", "") or "").strip()[:80],
        "mine": request.GET.get("mine") == "1",
    }
    if filters["status"] not in SupportTicket.Status.values:
        filters["status"] = ""
    if filters["category"] not in SupportTicket.Category.values:
        filters["category"] = ""

    queryset = selectors.staff_tickets(
        status=filters["status"],
        category=filters["category"],
        q=filters["q"],
        assigned_to=request.user if filters["mine"] else None,
    )
    page = Paginator(queryset, settings.SUPPORT_TICKETS_PER_PAGE).get_page(request.GET.get("page"))
    context = {
        "page": page,
        "filters": filters,
        "counts": selectors.desk_counts(request.user),
        "status_choices": [("", "All"), *list(SupportTicket.Status.choices)],
        "category_choices": [("", "All"), *list(SupportTicket.Category.choices)],
    }
    return render(request, "support/staff_dashboard.html", context)


def _staff_context(ticket) -> dict:
    """Everything the desk needs on one ticket, in one pass."""
    agents = list(staff_with_capability(SUPPORT_AGENT))
    assigned_email = ticket.assigned_to.email if ticket.assigned_to_id else ""
    return {
        "ticket": ticket,
        "transcript": selectors.staff_messages(ticket),
        "events": selectors.ticket_events(ticket),
        "milestones": _milestones(ticket),
        "assign_form": StaffAssignForm(agents=agents, initial={"agent_email": assigned_email}),
        "status_form": StaffStatusForm(initial={"status": ticket.status}),
        "priority_form": StaffPriorityForm(initial={"priority": ticket.priority}),
        "escalate_form": StaffEscalateForm(),
        "note_form": StaffNoteForm(),
        "reply_form": MessageForm(),
        "link_form": StaffLinkForm(
            initial={
                "order": ticket.order_id,
                "payment": ticket.payment_id,
                "shipment": ticket.shipment_id,
                "product": ticket.product_id,
                "review": ticket.review_id,
                "loop_item": ticket.loop_item_id,
                "quest": ticket.quest_id,
            }
        ),
        "agent_count": len(agents),
    }


@login_required
@never_cache
@require_GET
def staff_ticket_detail(request, number: str):
    """``/support/staff/tickets/<number>/`` -- transcript, audit and every desk action."""
    _require_agent(request.user)
    ticket = selectors.staff_ticket(number)
    return render(request, "support/staff_ticket_detail.html", _staff_context(ticket))


def _staff_ticket(request, number: str):
    _require_agent(request.user)
    return selectors.staff_ticket(number)


@login_required
@never_cache
@require_POST
def staff_assign(request, number: str):
    ticket = _staff_ticket(request, number)
    form = StaffAssignForm(request.POST, agents=list(staff_with_capability(SUPPORT_AGENT)))
    if not form.is_valid():
        messages.error(request, "Choose a member of the support team.")
        return redirect("support:staff-ticket-detail", number=number)
    email = form.cleaned_data.get("agent_email") or ""
    agent = get_user_model().objects.filter(email=email).first() if email else None
    try:
        ticket_service.assign_ticket(ticket, actor=request.user, agent=agent, request=request)
    except SupportError as exc:
        return _post_error(request, exc, number, staff=True)
    messages.success(request, "Ticket updated.")
    return redirect("support:staff-ticket-detail", number=number)


@login_required
@never_cache
@require_POST
def staff_status(request, number: str):
    ticket = _staff_ticket(request, number)
    form = StaffStatusForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a valid status.")
        return redirect("support:staff-ticket-detail", number=number)
    target = form.cleaned_data["status"]
    if target == SupportTicket.Status.ESCALATED:
        messages.error(request, "Use the escalation form to escalate a ticket.")
        return redirect("support:staff-ticket-detail", number=number)
    try:
        ticket_service.transition_ticket(
            ticket, to_status=target, actor=request.user, request=request
        )
    except SupportError as exc:
        return _post_error(request, exc, number, staff=True)
    messages.success(request, "Status updated.")
    return redirect("support:staff-ticket-detail", number=number)


@login_required
@never_cache
@require_POST
def staff_priority(request, number: str):
    ticket = _staff_ticket(request, number)
    form = StaffPriorityForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a valid priority.")
        return redirect("support:staff-ticket-detail", number=number)
    try:
        ticket_service.set_priority(
            ticket, actor=request.user, priority=form.cleaned_data["priority"]
        )
    except SupportError as exc:
        return _post_error(request, exc, number, staff=True)
    messages.success(request, "Priority updated.")
    return redirect("support:staff-ticket-detail", number=number)


@login_required
@never_cache
@require_POST
def staff_escalate(request, number: str):
    ticket = _staff_ticket(request, number)
    form = StaffEscalateForm(request.POST)
    if not form.is_valid():
        messages.error(request, "An escalation needs a reason.")
        return redirect("support:staff-ticket-detail", number=number)
    try:
        ticket_service.escalate_ticket(
            ticket,
            actor=request.user,
            reason=form.cleaned_data["reason"],
            target=form.cleaned_data.get("target", ""),
            request=request,
        )
    except SupportError as exc:
        return _post_error(request, exc, number, staff=True)
    messages.success(request, "Ticket escalated.")
    return redirect("support:staff-ticket-detail", number=number)


@login_required
@never_cache
@require_POST
def staff_link(request, number: str):
    ticket = _staff_ticket(request, number)
    form = StaffLinkForm(request.POST)
    if not form.is_valid():
        messages.error(request, "That record could not be linked.")
        return redirect("support:staff-ticket-detail", number=number)
    try:
        changed = ticket_service.link_ticket_references(
            ticket, actor=request.user, **form.references()
        )
    except SupportError as exc:
        return _post_error(request, exc, number, staff=True)
    if changed:
        messages.success(request, "Records linked.")
    else:
        messages.info(request, "Nothing to change.")
    return redirect("support:staff-ticket-detail", number=number)


@login_required
@never_cache
@require_POST
def staff_note(request, number: str):
    """Internal note: invisible to the customer, and never emailed to them."""
    ticket = _staff_ticket(request, number)
    form = StaffNoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Write a note first.")
        return redirect("support:staff-ticket-detail", number=number)
    try:
        message_service.post_agent_message(
            ticket,
            actor=request.user,
            body=form.cleaned_data["body"],
            internal=True,
            uploads=request.FILES.getlist("files"),
            request=request,
        )
    except SupportError as exc:
        return _post_error(request, exc, number, staff=True)
    messages.success(request, "Note added.")
    return redirect("support:staff-ticket-detail", number=number)


@login_required
@never_cache
@require_POST
def staff_reply(request, number: str):
    """A public reply from the desk: the customer is emailed, the ticket waits on them."""
    ticket = _staff_ticket(request, number)
    form = MessageForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Write a message first.")
        return redirect("support:staff-ticket-detail", number=number)
    try:
        message_service.post_agent_message(
            ticket,
            actor=request.user,
            body=form.cleaned_data["body"],
            internal=False,
            uploads=request.FILES.getlist("files"),
            request=request,
        )
    except SupportError as exc:
        return _post_error(request, exc, number, staff=True)
    messages.success(request, "Reply sent.")
    return redirect("support:staff-ticket-detail", number=number)
