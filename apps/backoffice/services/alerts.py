"""Deterministic operations alerts (Phase 16).

An alert is a **count against a configured threshold**, never a probability, a forecast or
a "you might want to look at". Every rule below is a single query over rows that already
exist, so the panel is always reproducible: the same database returns the same alerts in
the same order, with no clock-dependent randomness and no sampling.

Severity is carried as a *word* (``critical`` / ``warning`` / ``info``) that the template
renders next to the colour, because a panel that distinguishes severity only by hue is
unreadable to a colour-blind operator and invisible in a monochrome export.

Each alert also carries the capability that must see it: an Order Manager is not shown
inventory alerts, and the alerts page filters rather than offering a link that 404s.
"""

from __future__ import annotations

from datetime import timedelta
from typing import NamedTuple

from django.conf import settings
from django.db.models import F, FloatField
from django.db.models.functions import Cast
from django.utils import timezone

from apps.backoffice.permissions import (
    INVENTORY_VIEW,
    MARKETING_VIEW,
    MODERATION_VIEW,
    ORDERS_VIEW,
    PAYMENTS_VIEW,
    SUPPORT_VIEW,
    has,
)
from apps.drops.models import DropStatus, FlashDrop
from apps.engagement.models import Promotion, Review
from apps.inventory.models import Reservation, Stock
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.support.models import SupportTicket

__all__ = ["CRITICAL", "INFO", "SEVERITY_LABELS", "WARNING", "Alert", "alerts_for"]

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

#: Rendered next to the colour so severity is never colour-only.
SEVERITY_LABELS = {
    CRITICAL: "Critical",
    WARNING: "Warning",
    INFO: "Info",
}

_SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}


class Alert(NamedTuple):
    """One rule that fired: what it is, how many, why it matters, where to go."""

    key: str
    severity: str
    title: str
    count: int
    detail: str
    url_name: str
    capability: str

    @property
    def label(self) -> str:
        return SEVERITY_LABELS.get(self.severity, self.severity.title())


def _noun(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _evaluate() -> list[Alert]:
    """Run every rule. Deterministic: counts, then a fixed sort -- no randomness."""
    now = timezone.now()
    today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    threshold = settings.BACKOFFICE_LOW_STOCK_THRESHOLD
    alerts: list[Alert] = []

    def add(count: int, **kwargs) -> None:
        if count > 0:
            alerts.append(Alert(count=count, **kwargs))

    # --- inventory ---------------------------------------------------------------------
    add(
        Stock.objects.filter(on_hand=0).count(),
        key="out_of_stock",
        severity=CRITICAL,
        title="Variants out of stock",
        detail="nothing left to sell.",
        url_name="backoffice:inventory",
        capability=INVENTORY_VIEW,
    )
    add(
        Stock.objects.filter(on_hand__gt=0, on_hand__lte=threshold).count(),
        key="low_stock",
        severity=WARNING,
        title="Variants below the low-stock threshold",
        detail=f"at {threshold} or fewer.",
        url_name="backoffice:inventory",
        capability=INVENTORY_VIEW,
    )
    add(
        Reservation.objects.filter(
            status=Reservation.Status.ACTIVE, expires_at__lt=now
        ).count(),
        key="stale_holds",
        severity=WARNING,
        title="Stock holds past their expiry",
        detail="expired but still counted as reserved.",
        url_name="backoffice:inventory",
        capability=INVENTORY_VIEW,
    )

    # --- payments ----------------------------------------------------------------------
    failed_today = Payment.objects.filter(
        status=Payment.Status.FAILED, updated_at__gte=now - timedelta(days=1)
    ).count()
    add(
        failed_today,
        key="payment_failures",
        severity=CRITICAL,
        title="Payments failed in the last 24 hours",
        detail="customers could not check out.",
        url_name="backoffice:payments",
        capability=PAYMENTS_VIEW,
    )
    add(
        Payment.objects.filter(
            status__in=(Payment.Status.CREATED, Payment.Status.PENDING),
            updated_at__lt=now
            - timedelta(minutes=settings.BACKOFFICE_STALE_PAYMENT_MINUTES),
        ).count(),
        key="stale_payments",
        severity=WARNING,
        title="Payments stuck in progress",
        detail=(
            f"not moved for {settings.BACKOFFICE_STALE_PAYMENT_MINUTES} minutes."
        ),
        url_name="backoffice:payments",
        capability=PAYMENTS_VIEW,
    )

    # --- orders ------------------------------------------------------------------------
    add(
        Order.objects.filter(
            status__in=(Order.Status.PAID, Order.Status.PROCESSING),
            paid_at__lt=now - timedelta(hours=24),
        ).count(),
        key="unfulfilled_orders",
        severity=WARNING,
        title="Paid orders waiting over a day",
        detail="paid for over 24 hours without shipping.",
        url_name="backoffice:orders",
        capability=ORDERS_VIEW,
    )
    add(
        Order.objects.filter(
            status=Order.Status.CANCELLED, cancelled_at__gte=today
        ).count(),
        key="cancelled_orders",
        severity=WARNING,
        title="Cancellations today",
        detail="cancelled today; the alert fires at any.",
        url_name="backoffice:orders",
        capability=ORDERS_VIEW,
    )
    add(
        Order.objects.filter(
            status=Order.Status.PENDING_PAYMENT, created_at__lt=now - timedelta(days=2)
        ).count(),
        key="unpaid_orders",
        severity=INFO,
        title="Orders left unpaid for two days",
        detail="still waiting for payment.",
        url_name="backoffice:orders",
        capability=ORDERS_VIEW,
    )

    # --- support -----------------------------------------------------------------------
    open_statuses = tuple(
        status
        for status in SupportTicket.Status.values
        if status not in (SupportTicket.Status.RESOLVED, SupportTicket.Status.CLOSED)
    )
    add(
        SupportTicket.objects.filter(
            status__in=open_statuses, escalated_at__isnull=False
        ).count(),
        key="escalated_tickets",
        severity=CRITICAL,
        title="Escalated tickets open",
        detail="escalated and not yet closed.",
        url_name="backoffice:support",
        capability=SUPPORT_VIEW,
    )
    add(
        SupportTicket.objects.filter(
            status__in=open_statuses,
            last_customer_message_at__isnull=False,
            last_customer_message_at__lt=now
            - timedelta(days=settings.BACKOFFICE_SLA_WAITING_DAYS),
            last_agent_message_at__isnull=True,
        ).count(),
        key="stale_tickets",
        severity=WARNING,
        title="Tickets past their first-response window",
        detail=(
            f"waiting over {settings.BACKOFFICE_SLA_WAITING_DAYS} days for a first reply."
        ),
        url_name="backoffice:support",
        capability=SUPPORT_VIEW,
    )

    # --- moderation ---------------------------------------------------------------------
    pending_reviews = Review.objects.filter(status=Review.Status.PENDING).count()
    add(
        pending_reviews,
        key="review_backlog",
        severity=WARNING if pending_reviews >= settings.BACKOFFICE_REVIEW_BACKLOG else INFO,
        title="Reviews waiting for moderation",
        detail=f"pending; the backlog alert is at {settings.BACKOFFICE_REVIEW_BACKLOG}.",
        url_name="backoffice:reviews",
        capability=MODERATION_VIEW,
    )

    # --- merchandising -------------------------------------------------------------------
    add(
        FlashDrop.objects.filter(
            status=DropStatus.LIVE,
            ends_at__lte=now + timedelta(hours=settings.BACKOFFICE_DROP_ENDING_HOURS),
        ).count(),
        key="drop_ending",
        severity=INFO,
        title="Drops ending soon",
        detail=f"ends within {settings.BACKOFFICE_DROP_ENDING_HOURS} hours.",
        url_name="backoffice:drops",
        capability=MARKETING_VIEW,
    )
    add(
        Promotion.objects.filter(
            is_active=True, usage_limit__isnull=False, ends_at__gt=now
        )
        .annotate(used_fraction=Cast("used_count", FloatField()) / F("usage_limit"))
        .filter(used_fraction__gte=settings.BACKOFFICE_PROMOTION_LIMIT_FRACTION)
        .count(),
        key="promotion_limit",
        severity=WARNING,
        title="Promotions near their usage limit",
        detail=(
            f"{int(settings.BACKOFFICE_PROMOTION_LIMIT_FRACTION * 100)}% of the limit used."
        ),
        url_name="backoffice:promotions",
        capability=MARKETING_VIEW,
    )

    alerts.sort(key=lambda alert: (_SEVERITY_ORDER[alert.severity], alert.key))
    return alerts


def alerts_for(user) -> list[Alert]:
    """Every rule that fired, minus the ones this user is not entitled to see."""
    return [alert for alert in _evaluate() if has(user, alert.capability)]
