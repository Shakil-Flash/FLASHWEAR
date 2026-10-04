"""Serializers for the order API.

Hand-written for the same reason as the account ones: an order exposes a
*snapshot* (names, prices, address as text), so the payload is assembled from
those frozen columns rather than joined live to the catalogue. Payment appears
only as a status summary -- never a provider reference, never anything a client
could replay.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.orders.models import Order, OrderAddress, OrderItem, Shipment
from apps.payments.models import Payment

__all__ = [
    "OrderAddressSerializer",
    "OrderDetailSerializer",
    "OrderItemSerializer",
    "OrderListSerializer",
]


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = (
            "sku",
            "product_name",
            "option_label",
            "quantity",
            "unit_price",
            "line_total",
        )


class OrderAddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderAddress
        fields = (
            "full_name",
            "phone",
            "line1",
            "line2",
            "city",
            "region",
            "postal_code",
            "country",
        )


class PaymentSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ("status", "provider", "amount", "currency", "paid_at")
        read_only_fields = fields


class ShipmentSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Shipment
        fields = ("status", "method", "carrier", "tracking_number", "shipped_at", "delivered_at")
        read_only_fields = fields


class OrderListSerializer(serializers.ModelSerializer):
    """Compact row for the history list."""

    payment_status = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            "number",
            "status",
            "currency",
            "subtotal",
            "shipping_amount",
            "total",
            "item_count",
            "payment_status",
            "created_at",
        )
        read_only_fields = fields

    def get_payment_status(self, obj: Order) -> str | None:
        payment = getattr(obj, "payment", None)
        return payment.status if payment else None

    def get_item_count(self, obj: Order) -> int:
        # Prefetched for the list; falls back to a count for a single object.
        items = getattr(obj, "_prefetched_objects_cache", {}).get("items")
        return sum(item.quantity for item in items) if items is not None else obj.items.count()


class OrderDetailSerializer(serializers.ModelSerializer):
    """Everything a client needs to render one order, all from the snapshot."""

    items = OrderItemSerializer(many=True)
    shipping_address = OrderAddressSerializer()
    payment = PaymentSummarySerializer(read_only=True, allow_null=True)
    shipments = ShipmentSummarySerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = (
            "number",
            "status",
            "currency",
            "subtotal",
            "shipping_amount",
            "discount_amount",
            "tax_amount",
            "total",
            "shipping_code",
            "shipping_name",
            "shipping_estimate",
            "items",
            "shipping_address",
            "payment",
            "shipments",
            "created_at",
            "paid_at",
            "shipped_at",
            "delivered_at",
            "cancelled_at",
        )
        read_only_fields = fields
