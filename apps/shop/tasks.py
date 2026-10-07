"""Celery background tasks for shop & wishlist (Phase 33)."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger("flashwear.shop.tasks")

__all__ = ["sweep_back_in_stock_alerts", "sweep_price_drop_alerts"]


@shared_task(name="shop.sweep_price_drop_alerts")
def sweep_price_drop_alerts() -> int:
    """Scan and dispatch price drop alerts across all watched items. Never raises."""
    try:
        from apps.shop.wishlist_services import check_and_trigger_price_drop_alerts

        return check_and_trigger_price_drop_alerts()
    except Exception:
        logger.exception("shop.sweep_price_drop_alerts failed")
        return 0


@shared_task(name="shop.sweep_back_in_stock_alerts")
def sweep_back_in_stock_alerts() -> int:
    """Scan and dispatch back-in-stock alerts across all watched items. Never raises."""
    try:
        from apps.shop.wishlist_services import check_and_trigger_back_in_stock_alerts

        return check_and_trigger_back_in_stock_alerts()
    except Exception:
        logger.exception("shop.sweep_back_in_stock_alerts failed")
        return 0
