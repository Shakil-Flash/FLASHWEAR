"""Order API: the signed-in customer's own history.

Session-authenticated like the rest of the account API, and scoped through
``request.user`` in the queryset itself so an order number in the URL can only
ever resolve to its owner (someone else's number is a 404, never a 403 that
confirms the id exists).
"""

from __future__ import annotations

from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import IsAuthenticated

from apps.orders.serializers import OrderDetailSerializer, OrderListSerializer

__all__ = ["OrderDetailView", "OrderListView"]


def _own_orders(user):
    return (
        user.orders.select_related("payment")
        .prefetch_related("items", "shipments")
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
