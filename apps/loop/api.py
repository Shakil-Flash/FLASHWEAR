"""FLASH Loop API (``/api/v1/loop/``).

Conventions copied from the rest of the API surface:

* public reads declare ``permission_classes = [AllowAny]`` explicitly
  and serve only what :mod:`apps.loop.selectors` yields;
* customer writes are session-authenticated; object access is scoped
  to ``request.user`` at the queryset level, so another customer's
  row 404s (existence is not disclosed);
* every domain failure (ownership, eligibility, illegal transition)
  maps to a structured ``{"detail", "code"}`` payload -- the messages
  are already written to be safe for a client to display and never
  reveal *why* ownership failed beyond "we could not verify";
* no ``status`` / ``authenticity`` / ``seller`` field is ever read
  from a payload: those are service-layer facts.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.generics import ListAPIView, ListCreateAPIView, RetrieveAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.loop import selectors
from apps.loop.models import LoopItem
from apps.loop.serializers import (
    LoopCreditSerializer,
    LoopItemCreateSerializer,
    LoopItemSerializer,
    RecycleRequestCreateSerializer,
    RecycleRequestSerializer,
    ResaleListingDetailSerializer,
    ResaleListingListSerializer,
    TradeInRequestCreateSerializer,
    TradeInRequestSerializer,
)
from apps.loop.services import loop_items as loop_services
from apps.loop.services.errors import LoopError

__all__ = [
    "LoopCreditListView",
    "LoopItemCancelView",
    "LoopItemDetailView",
    "LoopItemListCreateView",
    "LoopItemSubmitView",
    "LoopRootView",
    "RecycleListCreateView",
    "ResaleListingDetailView",
    "ResaleListingListView",
    "TradeInListCreateView",
]


def _loop_error(exc: LoopError) -> Response:
    """Map a domain error to a deterministic, non-leaky payload."""
    return Response(
        {"detail": exc.message, "code": exc.code},
        status=status.HTTP_400_BAD_REQUEST,
    )


class LoopPagination(PageNumberPagination):
    """Client-controllable page size, clamped exactly like the catalogue.

    Without the ceiling, ``?page_size=100000`` turns a loop endpoint
    into a full dump of someone's (or everyone's) rows.
    """

    page_size_query_param = "page_size"
    max_page_size = settings.CATALOG_API_MAX_PAGE_SIZE
    page_size = settings.CATALOG_API_PAGE_SIZE


class ResaleListingListView(ListAPIView):
    """``GET /api/v1/loop/resale/`` -- the public shelf.

    ``?sort=``, ``?category=``, ``?size=``, ``?color=``,
    ``?condition=``, ``?material=``, ``?min_price=``,
    ``?max_price=``, ``?q=`` and ``?page_size=``. Unknown values
    narrow to nothing rather than 400, so stale bookmarks stay calm.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = ResaleListingListSerializer
    pagination_class = LoopPagination

    def get_queryset(self):
        params = self.request.query_params

        def decimal(key: str) -> Decimal | None:
            raw = (params.get(key) or "").strip()
            if not raw:
                return None
            try:
                return Decimal(raw)
            except Exception:
                return None

        sort = (params.get("sort") or "").strip() or selectors.DEFAULT_PUBLIC_SORT
        if sort not in selectors.PUBLIC_SORT_CHOICES:
            sort = selectors.DEFAULT_PUBLIC_SORT

        return selectors.public_resale_listings(
            sort=sort,
            category=(params.get("category") or "").strip().lower(),
            size=(params.get("size") or "").strip(),
            color=(params.get("color") or "").strip().lower(),
            condition=(params.get("condition") or "").strip().lower(),
            material=(params.get("material") or "").strip().lower(),
            min_price=decimal("min_price"),
            max_price=decimal("max_price"),
            q=(params.get("q") or "").strip(),
        )


class ResaleListingDetailView(RetrieveAPIView):
    """``GET /api/v1/loop/resale/<slug>/`` -- one listing, public facts only."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = ResaleListingDetailSerializer
    lookup_field = "slug"

    def get_queryset(self):
        return selectors.public_resale_listings()


class LoopItemListCreateView(ListCreateAPIView):
    """``GET/POST /api/v1/loop/items/`` -- the customer's own loop items.

    POST creates a ``DRAFT`` after server-side ownership verification;
    submission happens at ``/items/<id>/submit/`` so review (photos,
    wording) can happen in between.
    """

    permission_classes = [IsAuthenticated]
    pagination_class = LoopPagination

    def get_serializer_class(self):
        if self.request.method == "POST":
            return LoopItemCreateSerializer
        return LoopItemSerializer

    def get_queryset(self):
        return selectors.user_loop_items(self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            item = loop_services.create_loop_item(
                request.user,
                data["type"],
                condition=data["condition"],
                condition_notes=data.get("condition_notes", ""),
                title=data.get("title", ""),
                description=data.get("description", ""),
                asking_price=data.get("asking_price"),
                **serializer.ownership_kwargs(),
            )
        except LoopError as exc:
            return _loop_error(exc)
        return Response(
            LoopItemSerializer(item, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


class LoopItemDetailView(RetrieveAPIView):
    """``GET /api/v1/loop/items/<id>/`` -- owner (or staff) only.

    Scoped by user at the queryset level: someone else's id 404s
    instead of 403ing, so the URL space is not probeable.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = LoopItemSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return LoopItem.objects.all()
        return selectors.user_loop_items(user)


class LoopItemSubmitView(APIView):
    """``POST /api/v1/loop/items/<id>/submit/`` -- DRAFT -> SUBMITTED."""

    permission_classes = [IsAuthenticated]

    def _item(self, request, pk: int) -> LoopItem:
        user = request.user
        queryset = LoopItem.objects.all() if user.is_staff else selectors.user_loop_items(user)
        try:
            return queryset.get(pk=pk)
        except LoopItem.DoesNotExist:
            raise NotFound("No loop item matches the given id.") from None

    def post(self, request, pk: int):
        item = self._item(request, pk)
        try:
            loop_services.submit_loop_item(item, actor=request.user)
        except LoopError as exc:
            return _loop_error(exc)
        item.refresh_from_db()
        return Response(LoopItemSerializer(item).data)


class LoopItemCancelView(APIView):
    """``POST /api/v1/loop/items/<id>/cancel/`` -- owner cancels own item."""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        user = request.user
        queryset = LoopItem.objects.all() if user.is_staff else selectors.user_loop_items(user)
        try:
            item = queryset.get(pk=pk)
        except LoopItem.DoesNotExist:
            return Response(
                {"detail": "No loop item matches the given id.", "code": "loop_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            loop_services.cancel_loop_item(item, actor=request.user)
        except LoopError as exc:
            return _loop_error(exc)
        item.refresh_from_db()
        return Response(LoopItemSerializer(item).data)


class TradeInListCreateView(ListCreateAPIView):
    """``GET/POST /api/v1/loop/trade-ins/`` -- customer-scoped.

    POST creates *and submits* a trade-in in one call (the trade-in
    path has no draft review step for the customer; the estimate is
    computed server-side at creation).
    """

    permission_classes = [IsAuthenticated]
    pagination_class = LoopPagination

    def get_serializer_class(self):
        if self.request.method == "POST":
            return TradeInRequestCreateSerializer
        return TradeInRequestSerializer

    def get_queryset(self):
        return selectors.user_trade_ins(self.request.user).order_by("-created_at")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            item = loop_services.create_loop_item(
                request.user,
                LoopItem.Type.TRADE_IN,
                condition=data["condition"],
                condition_notes=data.get("condition_notes", ""),
                **serializer.ownership_kwargs(),
            )
            loop_services.submit_loop_item(item, actor=request.user)
        except LoopError as exc:
            return _loop_error(exc)
        request_row = item.trade_in_request
        return Response(
            TradeInRequestSerializer(request_row).data,
            status=status.HTTP_201_CREATED,
        )


class RecycleListCreateView(ListCreateAPIView):
    """``GET/POST /api/v1/loop/recycling/`` -- customer-scoped."""

    permission_classes = [IsAuthenticated]
    pagination_class = LoopPagination

    def get_serializer_class(self):
        if self.request.method == "POST":
            return RecycleRequestCreateSerializer
        return RecycleRequestSerializer

    def get_queryset(self):
        return selectors.user_recycle_requests(self.request.user).order_by("-created_at")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            item = loop_services.create_loop_item(
                request.user,
                LoopItem.Type.RECYCLE,
                condition=data["condition"],
                condition_notes=data.get("condition_notes", ""),
                **serializer.ownership_kwargs(),
            )
            loop_services.submit_loop_item(item, actor=request.user)
        except LoopError as exc:
            return _loop_error(exc)
        request_row = item.recycle_request
        return Response(
            RecycleRequestSerializer(request_row).data,
            status=status.HTTP_201_CREATED,
        )


class LoopCreditListView(ListAPIView):
    """``GET /api/v1/loop/credits/`` -- the customer's valid loop credits."""

    permission_classes = [IsAuthenticated]
    serializer_class = LoopCreditSerializer
    pagination_class = LoopPagination

    def get_queryset(self):
        return selectors.user_credits(self.request.user)


class LoopRootView(APIView):
    """``GET /api/v1/loop/`` -- endpoint index (matches every other root)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request):
        return Response(
            {
                "resale": request.build_absolute_uri("/api/v1/loop/resale/"),
                "items": request.build_absolute_uri("/api/v1/loop/items/"),
                "trade_ins": request.build_absolute_uri("/api/v1/loop/trade-ins/"),
                "recycling": request.build_absolute_uri("/api/v1/loop/recycling/"),
                "credits": request.build_absolute_uri("/api/v1/loop/credits/"),
            }
        )
