"""Order API: the signed-in customer's own history.

Session-authenticated like the rest of the account API, and scoped through
``request.user`` in the queryset itself so an order number in the URL can only
ever resolve to its owner (someone else's number is a 404, never a 403 that
confirms the id exists).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.orders.models import InvalidTransition, ReturnRequest
from apps.orders.returns_services import (
    cancel_return_request,
    check_order_return_eligibility,
    create_return_request,
)
from apps.orders.serializers import (
    OrderDetailSerializer,
    OrderListSerializer,
    ReturnRequestCreateSerializer,
    ReturnRequestSerializer,
)

__all__ = [
    "OrderDetailView",
    "OrderListView",
    "OrderReturnCreateView",
    "OrderReturnEligibilityView",
    "ReturnCancelView",
    "ReturnDetailView",
    "ReturnListView",
]


def _own_orders(user):
    return (
        user.orders.select_related("payment")
        .prefetch_related("items", "shipments")
        .order_by("-created_at")
    )


def _own_returns(user):
    return (
        user.returns.select_related("order")
        .prefetch_related("items__order_item", "refunds", "events")
        .order_by("-created_at")
    )


class OrderListView(ListAPIView):
    """``GET /api/v1/orders/`` -- order history, newest first, paginated."""

    permission_classes = [IsAuthenticated]
    serializer_class = OrderListSerializer

    def get_queryset(self):
        return _own_orders(self.request.user)

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class OrderDetailView(RetrieveAPIView):
    """``GET /api/v1/orders/<number>/`` -- one order in full."""

    permission_classes = [IsAuthenticated]
    serializer_class = OrderDetailSerializer
    lookup_field = "number"

    def get_queryset(self):
        return _own_orders(self.request.user)

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class OrderReturnEligibilityView(APIView):
    """``GET /api/v1/orders/<number>/return-eligibility/``

    Server-side return eligibility check for the signed-in customer's order.
    """

    permission_classes = [IsAuthenticated]

    @method_decorator(never_cache)
    def get(self, request, number: str, *args, **kwargs):
        order = get_object_or_404(_own_orders(request.user), number=number)
        eligibility = check_order_return_eligibility(order)
        return Response(eligibility)


class OrderReturnCreateView(APIView):
    """``POST /api/v1/orders/<number>/returns/``

    Submit a return or exchange request for eligible items on the order.
    """

    permission_classes = [IsAuthenticated]

    @method_decorator(never_cache)
    def post(self, request, number: str, *args, **kwargs):
        order = get_object_or_404(_own_orders(request.user), number=number)
        serializer = ReturnRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        try:
            return_request = create_return_request(
                order=order,
                user=request.user,
                items_data=data["items"],
                return_type=data.get("return_type", ReturnRequest.ReturnType.REFUND),
                reason=data.get("reason", ReturnRequest.Reason.SIZE_FIT),
                customer_note=data.get("customer_note", ""),
            )
        except ValidationError as exc:
            msg = exc.messages if hasattr(exc, "messages") else [str(exc)]
            return Response({"detail": " ".join(msg)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            ReturnRequestSerializer(return_request).data,
            status=status.HTTP_201_CREATED,
        )


class ReturnListView(ListAPIView):
    """``GET /api/v1/returns/`` -- customer's return requests history."""

    permission_classes = [IsAuthenticated]
    serializer_class = ReturnRequestSerializer

    def get_queryset(self):
        return _own_returns(self.request.user)

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ReturnDetailView(RetrieveAPIView):
    """``GET /api/v1/returns/<number>/`` -- one return request with items, events, refunds."""

    permission_classes = [IsAuthenticated]
    serializer_class = ReturnRequestSerializer
    lookup_field = "number"

    def get_queryset(self):
        return _own_returns(self.request.user)

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ReturnCancelView(APIView):
    """``POST /api/v1/returns/<number>/cancel/``

    Cancel a return request while it is still in REQUESTED status.
    """

    permission_classes = [IsAuthenticated]

    @method_decorator(never_cache)
    def post(self, request, number: str, *args, **kwargs):
        return_request = get_object_or_404(_own_returns(request.user), number=number)
        note = request.data.get("note", "Cancelled by customer")
        try:
            cancelled_ret = cancel_return_request(
                return_request=return_request,
                user=request.user,
                note=note,
            )
        except (ValidationError, InvalidTransition) as exc:
            msg = exc.messages if hasattr(exc, "messages") else [str(exc)]
            return Response({"detail": " ".join(msg)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(ReturnRequestSerializer(cancelled_ret).data)
