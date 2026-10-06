"""Serializers for the order API.

Hand-written for the same reason as the account ones: an order exposes a
*snapshot* (names, prices, address as text), so the payload is assembled from
those frozen columns rather than joined live to the catalogue. Payment appears
only as a status summary -- never a provider reference, never anything a client
could replay.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.orders.models import (
    Order,
    OrderAddress,
    OrderItem,
    ReturnEvent,
    ReturnItem,
    ReturnRequest,
    Shipment,
)
from apps.payments.models import Payment, Refund

__all__ = [
    "OrderAddressSerializer",
    "OrderDetailSerializer",
    "OrderItemSerializer",
    "OrderListSerializer",
    "RefundSummarySerializer",
    "ReturnEventSerializer",
    "ReturnItemCreateInputSerializer",
    "ReturnItemSerializer",
    "ReturnRequestCreateSerializer",
    "ReturnRequestSerializer",
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


class RefundSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Refund
        fields = (
            "number",
            "amount",
            "currency",
            "status",
            "reason",
            "is_shipping_refunded",
            "provider_reference",
            "processed_at",
            "created_at",
        )
        read_only_fields = fields


class ReturnEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = ReturnEvent
        fields = ("id", "event_type", "actor_name", "note", "metadata", "created_at")
        read_only_fields = fields

    def get_actor_name(self, obj: ReturnEvent) -> str | None:
        if not obj.actor:
            return None
        return obj.actor.get_full_name() or obj.actor.email or str(obj.actor)


class ReturnItemSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(source="order_item.sku", read_only=True)
    product_name = serializers.CharField(source="order_item.product_name", read_only=True)
    option_label = serializers.CharField(source="order_item.option_label", read_only=True)
    unit_price = serializers.DecimalField(
        source="order_item.unit_price", max_digits=10, decimal_places=2, read_only=True
    )
    replacement_variant_sku = serializers.CharField(
        source="replacement_variant.sku", read_only=True, default=None
    )
    replacement_variant_name = serializers.CharField(
        source="replacement_variant.product.name", read_only=True, default=None
    )

    class Meta:
        model = ReturnItem
        fields = (
            "id",
            "order_item_id",
            "sku",
            "product_name",
            "option_label",
            "unit_price",
            "quantity",
            "reason",
            "customer_note",
            "replacement_variant_id",
            "replacement_variant_sku",
            "replacement_variant_name",
            "price_difference",
            "received_quantity",
            "accepted_quantity",
            "rejected_quantity",
            "condition",
            "inspection_notes",
            "refund_amount",
            "is_restocked",
        )
        read_only_fields = fields


class ReturnRequestSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.number", read_only=True)
    is_cancellable = serializers.BooleanField(source="is_cancellable_by_customer", read_only=True)
    items = ReturnItemSerializer(many=True, read_only=True)
    events = ReturnEventSerializer(many=True, read_only=True)
    refunds = RefundSummarySerializer(many=True, read_only=True)

    class Meta:
        model = ReturnRequest
        fields = (
            "number",
            "order_number",
            "status",
            "return_type",
            "reason",
            "customer_note",
            "staff_note",
            "tracking_number",
            "is_cancellable",
            "items",
            "events",
            "refunds",
            "created_at",
            "updated_at",
            "approved_at",
            "received_at",
            "inspected_at",
            "refunded_at",
            "completed_at",
            "rejected_at",
            "cancelled_at",
        )
        read_only_fields = fields


class ReturnItemCreateInputSerializer(serializers.Serializer):
    order_item_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1)
    reason = serializers.ChoiceField(
        choices=ReturnRequest.Reason.choices,
        required=False,
        default=ReturnRequest.Reason.SIZE_FIT,
    )
    customer_note = serializers.CharField(required=False, allow_blank=True, max_length=300)
    replacement_variant_id = serializers.IntegerField(required=False, allow_null=True, default=None)


class ReturnRequestCreateSerializer(serializers.Serializer):
    return_type = serializers.ChoiceField(
        choices=ReturnRequest.ReturnType.choices,
        default=ReturnRequest.ReturnType.REFUND,
    )
    reason = serializers.ChoiceField(
        choices=ReturnRequest.Reason.choices,
        default=ReturnRequest.Reason.SIZE_FIT,
    )
    customer_note = serializers.CharField(
        required=False, allow_blank=True, max_length=1000, default=""
    )
    items = ReturnItemCreateInputSerializer(many=True, allow_empty=False)
