"""Receivers turning order events into FLASH Points movements (Phase 7).

Registered by ``EngagementConfig.ready()``. Both handlers are synchronous and idempotent --
they run inside the payment/cancellation transaction, so a failure here fails the whole block
(deliberately: a payment that cannot award the points it promised should not half-happen).

Idempotency is carried by the services' unique references (``order:<number>`` for redemption
and earn, conditional UPDATE for releases), which is what makes webhook replays and admin
retries safe.
"""

from __future__ import annotations

from django.dispatch import receiver

from apps.engagement.services import loyalty
from apps.orders.signals import order_cancelled, order_paid


@receiver(order_paid, dispatch_uid="engagement_points_on_paid")
def award_and_consume_points(sender, order, **kwargs) -> None:
    """Paid order: consume the redemption hold first, then award the purchase earn.

    Order matters only for readability -- the hold's negative row and the earn's positive row
    have different ``(reference, type)`` pairs, so neither can collide with the other.
    """
    loyalty.consume_points_for_order(order)
    loyalty.earn_points_for_order(order)


@receiver(order_cancelled, dispatch_uid="engagement_release_points_on_cancel")
def release_points_on_cancel(sender, order, **kwargs) -> None:
    """Unpaid order cancelled: hand any held points back (no-op when none were held)."""
    loyalty.release_points_for_order(order)
