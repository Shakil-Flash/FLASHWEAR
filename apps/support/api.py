"""FLASH Support API (``/api/v1/support/``).

Conventions copied from the rest of the API surface:

* session-authenticated writes; object access is scoped to ``request.user`` at the queryset
  level, so another customer's ticket number 404s rather than 403s (existence is not
  disclosed);
* the staff namespace answers 404 to anyone without the agent capability for exactly the
  same reason -- an API that says "403, you are not staff" has told a scanner where the
  desk is;
* every domain failure (illegal transition, duplicate, throttled, forbidden) maps to a
  structured ``{"detail", "code"}`` payload, with **429** for the rate limit and **403**
  for a capability the caller *does* have but not for this action;
* no ``status``, ``priority``, ``assignee`` or ``customer`` field is ever read from a
  payload: those are service-layer facts.

Attachments are downloaded through the HTML route (``/support/attachments/<pk>/``), which
already proves ownership per request; there is deliberately no second, parallel download
endpoint with its own rules.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.generics import (
    GenericAPIView,
    ListAPIView,
    ListCreateAPIView,
    RetrieveAPIView,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

from apps.support import selectors
from apps.support.models import SupportTicket
from apps.support.permissions import SUPPORT_AGENT, has_capability
from apps.support.serializers import (
    AssignSerializer,
    EscalateSerializer,
    LinkSerializer,
    MessageCreateSerializer,
    PrioritySerializer,
    StatusSerializer,
    SupportMessageSerializer,
    SupportTicketCreateSerializer,
    SupportTicketSerializer,
)
from apps.support.services import messages as message_service
from apps.support.services import tickets as ticket_service
from apps.support.services.errors import SupportError

__all__ = [
    "StaffTicketAssignView",
    "StaffTicketDetailView",
    "StaffTicketEscalateView",
    "StaffTicketLinkView",
    "StaffTicketListView",
    "StaffTicketNoteView",
    "StaffTicketPriorityView",
    "StaffTicketReplyView",
    "StaffTicketStatusView",
    "SupportPagination",
    "SupportRootView",
    "SupportTicketCloseView",
    "SupportTicketDetailView",
    "SupportTicketListCreateView",
    "SupportTicketMessagesView",
    "SupportTicketReopenView",
]


class SupportPagination(PageNumberPagination):
    """Clamped page size, exactly like the catalogue and the loop."""

    page_size = settings.CATALOG_API_PAGE_SIZE
    page_size_query_param = "page_size"
    max_page_size = settings.CATALOG_API_MAX_PAGE_SIZE


def support_error(exc: SupportError) -> Response:
    """Map a domain error to a deterministic, non-leaky payload."""
    if exc.code == "support_throttled":
        code = status.HTTP_429_TOO_MANY_REQUESTS
    elif exc.code == "support_forbidden":
        code = status.HTTP_403_FORBIDDEN
    else:
        code = status.HTTP_400_BAD_REQUEST
    return Response({"detail": exc.message, "code": exc.code}, status=code)


def require_agent(user) -> None:
    """Desk entry guard: no capability means the route does not exist for this caller."""
    if not has_capability(user, SUPPORT_AGENT):
        raise NotFound("No support ticket matches the given number.")


class SupportTicketListCreateView(ListCreateAPIView):
    """``GET/POST /api/v1/support/tickets/`` -- the customer's own tickets.

    POST opens a ticket exactly as the HTML form does, including duplicate detection and
    the per-user rate limit; files are accepted as multipart under the ``files`` key.
    """

    permission_classes = [IsAuthenticated]
    pagination_class = SupportPagination

    def get_serializer_class(self):
        if self.request.method == "POST":
            return SupportTicketCreateSerializer
        return SupportTicketSerializer

    def get_queryset(self):
        return selectors.user_tickets(self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            ticket = ticket_service.open_ticket(
                customer=request.user,
                subject=data["subject"],
                description=data["description"],
                category=data["category"],
                source=SupportTicket.Source.API,
                refs=serializer.references(),
                uploads=request.FILES.getlist("files"),
                request=request,
            )
        except SupportError as exc:
            return support_error(exc)
        # Re-read through the selector so the response carries the same annotation the
        # list endpoint would have produced.
        ticket = selectors.user_ticket(request.user, ticket.number)
        return Response(
            SupportTicketSerializer(ticket, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


class SupportTicketDetailView(RetrieveAPIView):
    """``GET /api/v1/support/tickets/<number>/`` -- owner only, 404 otherwise."""

    permission_classes = [IsAuthenticated]
    serializer_class = SupportTicketSerializer
    lookup_field = "number"

    def get_queryset(self):
        return selectors.user_tickets(self.request.user)


class SupportTicketMessagesView(ListCreateAPIView):
    """``GET/POST /api/v1/support/tickets/<number>/messages/`` -- the transcript.

    GET returns only what the customer may see; POST appends their reply. ``internal`` in
    the payload is ignored for a customer, and the database refuses the combination
    regardless of what any serializer did.
    """

    permission_classes = [IsAuthenticated]
    pagination_class = SupportPagination

    def get_serializer_class(self):
        if self.request.method == "POST":
            return MessageCreateSerializer
        return SupportMessageSerializer

    def get_ticket(self) -> SupportTicket:
        return selectors.user_ticket(self.request.user, self.kwargs["number"])

    def get_queryset(self):
        return selectors.user_messages(self.get_ticket())

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ticket = self.get_ticket()
        try:
            message = message_service.post_customer_message(
                ticket,
                customer=request.user,
                body=serializer.validated_data["body"],
                uploads=request.FILES.getlist("files"),
                request=request,
            )
        except SupportError as exc:
            return support_error(exc)
        return Response(
            SupportMessageSerializer(message, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


class _CustomerActionView(APIView):
    """Base for the customer's two state changes: close and reopen."""

    permission_classes = [IsAuthenticated]
    target = ""

    def post(self, request, number: str):
        ticket = selectors.user_ticket(request.user, number)
        try:
            if self.target == SupportTicket.Status.CLOSED:
                ticket_service.close_ticket(ticket, actor=request.user, request=request)
            else:
                ticket_service.reopen_ticket(ticket, actor=request.user, request=request)
        except SupportError as exc:
            return support_error(exc)
        return Response(SupportTicketSerializer(ticket, context={"request": request}).data)


class SupportTicketCloseView(_CustomerActionView):
    """``POST /api/v1/support/tickets/<number>/close/``."""

    target = SupportTicket.Status.CLOSED


class SupportTicketReopenView(_CustomerActionView):
    """``POST /api/v1/support/tickets/<number>/reopen/``."""

    target = SupportTicket.Status.IN_PROGRESS


# =============================================================================
# Desk namespace
# =============================================================================


class _StaffView:
    """Mixin: prove the capability, and mark the context as staff-only."""

    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        require_agent(request.user)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "staff": True}


class StaffTicketListView(_StaffView, ListAPIView):
    """``GET /api/v1/support/staff/tickets/`` -- the queue.

    Filters: ``?status=``, ``?category=``, ``?q=``, ``?mine=1``.
    """

    serializer_class = SupportTicketSerializer
    pagination_class = SupportPagination

    def get_queryset(self):
        params = self.request.query_params
        return selectors.staff_tickets(
            status=params.get("status", ""),
            category=params.get("category", ""),
            q=params.get("q", ""),
            assigned_to=self.request.user if params.get("mine") == "1" else None,
        )


class StaffTicketDetailView(_StaffView, RetrieveAPIView):
    """``GET /api/v1/support/staff/tickets/<number>/`` -- transcript and audit."""

    serializer_class = SupportTicketSerializer
    lookup_field = "number"

    def get_queryset(self):
        return selectors.staff_tickets()


class _StaffActionView(_StaffView, GenericAPIView):
    """Base for a desk action: resolve the ticket, run the service, map the failure.

    ``GenericAPIView`` rather than ``APIView`` because ``_StaffView`` adds the staff flag
    through ``get_serializer_context``, which only the generic views provide.
    """

    serializer_class = None

    def get_ticket(self) -> SupportTicket:
        return selectors.staff_ticket(self.kwargs["number"])

    def run(self, request, ticket, data) -> SupportTicket:
        raise NotImplementedError  # pragma: no cover - each action overrides this

    def post(self, request, number: str):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        ticket = self.get_ticket()
        try:
            self.run(request, ticket, serializer.validated_data)
        except SupportError as exc:
            return support_error(exc)
        return Response(SupportTicketSerializer(ticket, context=self.get_serializer_context()).data)


class StaffTicketAssignView(_StaffActionView):
    """``POST .../assign/``. Blank ``agent_email`` releases the ticket."""

    serializer_class = AssignSerializer

    def run(self, request, ticket, data):
        email = (data.get("agent_email") or "").strip()
        agent = None
        if email:
            agent = get_user_model().objects.filter(email=email, is_active=True).first()
        return ticket_service.assign_ticket(
            ticket, actor=request.user, agent=agent, request=request
        )


class StaffTicketStatusView(_StaffActionView):
    """``POST .../status/``. Escalation is routed elsewhere so it keeps its reason."""

    serializer_class = StatusSerializer

    def run(self, request, ticket, data):
        if data["status"] == SupportTicket.Status.ESCALATED:
            raise SupportError(
                "Use the escalation action to escalate a ticket.",
                code="support_use_escalate",
            )
        return ticket_service.transition_ticket(
            ticket, to_status=data["status"], actor=request.user, request=request
        )


class StaffTicketPriorityView(_StaffActionView):
    """``POST .../priority/``. No customer-facing equivalent exists."""

    serializer_class = PrioritySerializer

    def run(self, request, ticket, data):
        return ticket_service.set_priority(ticket, actor=request.user, priority=data["priority"])


class StaffTicketEscalateView(_StaffActionView):
    """``POST .../escalate/``. Manager capability required, reason mandatory."""

    serializer_class = EscalateSerializer

    def run(self, request, ticket, data):
        return ticket_service.escalate_ticket(
            ticket,
            actor=request.user,
            reason=data["reason"],
            target=data.get("target", ""),
            request=request,
        )


class StaffTicketLinkView(_StaffActionView):
    """``POST .../link/``. ``null`` unlinks a reference; an omitted key leaves it alone."""

    serializer_class = LinkSerializer

    def run(self, request, ticket, data):
        return ticket_service.link_ticket_references(ticket, actor=request.user, **data)


class StaffTicketNoteView(_StaffActionView):
    """``POST .../note/`` -- an internal note the customer will never see."""

    serializer_class = MessageCreateSerializer

    def run(self, request, ticket, data):
        return message_service.post_agent_message(
            ticket,
            actor=request.user,
            body=data["body"],
            internal=True,
            uploads=request.FILES.getlist("files"),
            request=request,
        )


class StaffTicketReplyView(_StaffActionView):
    """``POST .../reply/`` -- a public reply. ``internal`` may be set here."""

    serializer_class = MessageCreateSerializer

    def run(self, request, ticket, data):
        return message_service.post_agent_message(
            ticket,
            actor=request.user,
            body=data["body"],
            internal=bool(data.get("internal")),
            uploads=request.FILES.getlist("files"),
            request=request,
        )


class SupportRootView(APIView):
    """``GET /api/v1/support/`` -- endpoint index (matches every other root)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        def link(name: str) -> str:
            return request.build_absolute_uri(reverse(f"v1:{name}", request=request))

        return Response(
            {
                "tickets": link("support-ticket-list"),
                "staff_tickets": link("support-staff-ticket-list"),
            }
        )
