"""Conversion receivers: turn ``order_paid`` into analytics conversions.

Registered by :class:`apps.analytics.apps.AnalyticsConfig`. Both receivers are synchronous
and idempotent (the unique ``idempotency_key`` per order/drop makes webhook replays free),
matching how the engagement and quest receivers already treat the same signal. ``record_event``
never raises, so a payment can never be failed by analytics.
"""

from __future__ import annotations

from django.dispatch import receiver

from apps.analytics.services import record_event
from apps.orders.signals import order_paid


@receiver(order_paid, dispatch_uid="analytics_purchase")
def record_purchase(sender, order, **kwargs) -> None:
    """A captured payment is the purchase event -- the funnel's terminal step."""
    record_event(
        "purchase",
        user=order.user,
        object_type="order",
        object_id=order.pk,
        metadata={"total": str(order.total), "order_number": order.number},
        idempotency_key=f"purchase:{order.pk}",
    )
    _record_drop_purchases(order)


def _record_drop_purchases(order) -> None:
    """Derive ``drop_purchase`` from the order's products (drops share the normal checkout)."""
    from apps.drops.models import DropProduct

    product_ids = set(
        order.items.exclude(variant__isnull=True).values_list("variant__product_id", flat=True)
    )
    if not product_ids:
        return
    drop_ids = (
        DropProduct.objects.filter(product_id__in=product_ids)
        .values_list("drop_id", flat=True)
        .order_by()
        .distinct()
    )
    for drop_id in drop_ids:
        record_event(
            "drop_purchase",
            user=order.user,
            object_type="drop",
            object_id=drop_id,
            metadata={"order_number": order.number},
            idempotency_key=f"drop_purchase:{order.pk}:{drop_id}",
        )
