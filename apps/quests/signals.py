"""Wiring for the one event quest progress subscribes to (Phase 14).

Registered by :meth:`apps.quests.apps.QuestsConfig.ready()`. Like the Phase 7 loyalty
receivers, this runs synchronously inside the payment transaction -- but with the opposite
failure policy, and for a reason: FLASH Points *are* the payment promise (a failure there
must roll the payment back), while quest progress is a derived cache. If this receiver
blows up, the worst case is a counter that catches up on the user's next quest read, so
the exception is logged and swallowed rather than allowed to fail a real purchase.
"""

from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.orders.signals import order_paid
from apps.quests.services.events import record_event

logger = logging.getLogger(__name__)


@receiver(order_paid, dispatch_uid="quests_progress_on_paid")
def progress_on_order_paid(sender, order, **kwargs) -> None:
    """A paid order: re-derive purchase quests (and achievements) for the buyer."""
    try:
        record_event(order.user, "order_paid")
    except Exception:
        # Progress is re-derived on every read, so a missed event only delays the counter;
        # raising here would instead roll back the payment block that triggered it.
        logger.exception("Quest progress event failed for order %s", order.number)
