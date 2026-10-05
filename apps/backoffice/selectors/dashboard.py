"""The dashboard's numbers (Phase 16).

Every figure on ``/operations/`` is a real row count or a real sum over the orders,
stock, tickets, reviews and promotions that already exist -- no estimates, no mocked
series, no "sample data" branch. Each metric is one ``aggregate()`` call, so a dashboard
on ten orders and one on ten thousand both cost the same dozen queries.

Two conventions worth knowing:

* **Revenue** counts orders that reached ``paid`` and are not cancelled or refunded --
  money the shop still holds. Pending-payment and failed-payment totals are reported
  separately rather than folded in, because a "gross" that silently subtracts refunds is
  a number finance cannot reconcile.
* **Windows** are timezone-aware and half-open (``start <= x < end``) for every field,
  so "today" never double-counts a midnight row.
"""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Count, F, FloatField, Q, Sum
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from apps.backoffice.selectors.common import DateRange
from apps.backoffice.selectors.queues import open_support_statuses, pending_loop_count
from apps.core import health as health_checks
from apps.core import metrics as telemetry
from apps.drops.models import DropStatus, FlashDrop
from apps.engagement.models import PointsTransaction, Promotion, Review
from apps.inventory.models import Reservation, Stock
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.quests.models import Quest
from apps.support.models import SupportTicket

__all__ = ["REVENUE_STATUSES", "dashboard_summary", "health_report"]

#: Order states whose money is still the shop's: paid and moving through fulfilment.
REVENUE_STATUSES = (
    Order.Status.PAID,
    Order.Status.PROCESSING,
    Order.Status.SHIPPED,
    Order.Status.DELIVERED,
)


def _window(date_range: DateRange, field: str) -> dict:
    """``{field}__gte, {field}__lt}`` kwargs for a half-open window (empty for all time)."""
    if date_range.is_all_time:
        return {}
    return {f"{field}__gte": date_range.start, f"{field}__lt": date_range.end}


def _customer_counts(date_range: DateRange) -> dict:
    """Registered, newly registered and buying-in-window customers."""
    User = get_user_model()
    return {
        "total": User.objects.filter(is_active=True).count(),
        "new": User.objects.filter(**_window(date_range, "date_joined")).count(),
        "buying": Order.objects.filter(**_window(date_range, "created_at"))
        .values("user")
        .distinct()
        .count(),
    }


def dashboard_summary(*, date_range: DateRange) -> dict:
    """Every number the dashboard shows, grouped by the tile it feeds."""
    now = timezone.now()
    threshold = settings.BACKOFFICE_LOW_STOCK_THRESHOLD
    drop_window = now + timedelta(hours=settings.BACKOFFICE_DROP_ENDING_HOURS)
    sla_cutoff = now - timedelta(days=settings.BACKOFFICE_SLA_WAITING_DAYS)
    fraction = settings.BACKOFFICE_PROMOTION_LIMIT_FRACTION
    open_statuses = open_support_statuses()

    # --- sales ------------------------------------------------------------------------
    revenue = Order.objects.filter(
        **_window(date_range, "paid_at"), status__in=REVENUE_STATUSES
    ).aggregate(gross=Coalesce(Sum("total"), Decimal("0.00")), count=Count("pk"))
    placed = Order.objects.filter(**_window(date_range, "created_at")).aggregate(
        count=Count("pk"),
        discounts=Coalesce(Sum("discount_amount"), Decimal("0.00")),
    )
    gross = revenue["gross"]
    paid_count = revenue["count"]
    aov = (gross / paid_count).quantize(Decimal("0.01")) if paid_count else Decimal("0.00")

    pipeline = {
        row["status"]: row["n"]
        for row in Order.objects.values("status").annotate(n=Count("pk")).order_by()
    }

    # --- payments ---------------------------------------------------------------------
    payments = {
        "failed": Payment.objects.filter(status=Payment.Status.FAILED).count(),
        "pending": Payment.objects.filter(status=Payment.Status.PENDING).count(),
    }

    # --- customers --------------------------------------------------------------------
    customers = _customer_counts(date_range)

    # --- inventory --------------------------------------------------------------------
    stock = Stock.objects.annotate(qty_available=F("on_hand") - F("reserved")).aggregate(
        low=Count("pk", filter=Q(qty_available__gt=0, qty_available__lte=threshold)),
        out=Count("pk", filter=Q(qty_available=0)),
        reserved=Coalesce(Sum("reserved"), 0),
    )
    holds = Reservation.objects.aggregate(
        active=Count("pk", filter=Q(status=Reservation.Status.ACTIVE)),
        stale=Count(
            "pk",
            filter=Q(status=Reservation.Status.ACTIVE, expires_at__lt=now),
        ),
    )

    # --- support -----------------------------------------------------------------------
    support = SupportTicket.objects.aggregate(
        open=Count("pk", filter=Q(status__in=open_statuses)),
        urgent=Count(
            "pk", filter=Q(status__in=open_statuses, priority=SupportTicket.Priority.URGENT)
        ),
        escalated=Count(
            "pk",
            filter=Q(status__in=open_statuses, escalated_at__isnull=False),
        ),
        waiting=Count(
            "pk",
            filter=Q(
                status__in=open_statuses,
                last_customer_message_at__isnull=False,
                last_customer_message_at__lt=sla_cutoff,
                last_agent_message_at__isnull=True,
            ),
        ),
    )

    # --- community ---------------------------------------------------------------------
    loop = pending_loop_count()
    reviews = {"pending": Review.objects.filter(status=Review.Status.PENDING).count()}

    # --- merchandising ------------------------------------------------------------------
    drops = {
        row["status"]: row["n"]
        for row in FlashDrop.objects.values("status").annotate(n=Count("pk")).order_by()
    }
    drops["ending_soon"] = FlashDrop.objects.filter(
        status=DropStatus.LIVE, ends_at__lte=drop_window
    ).count()
    quests = {
        row["publish_state"]: row["n"]
        for row in Quest.objects.values("publish_state").annotate(n=Count("pk")).order_by()
    }
    promotions = Promotion.objects.filter(is_active=True).aggregate(
        running=Count("pk", filter=Q(starts_at__lte=now, ends_at__gt=now)),
        scheduled=Count("pk", filter=Q(starts_at__gt=now)),
    )
    promotions["near_limit"] = (
        Promotion.objects.filter(is_active=True, usage_limit__isnull=False, ends_at__gt=now)
        .annotate(used_fraction=Cast("used_count", FloatField()) / F("usage_limit"))
        .filter(used_fraction__gte=fraction)
        .count()
    )

    # --- FLASH Points ---------------------------------------------------------------------
    points = PointsTransaction.objects.filter(**_window(date_range, "created_at")).aggregate(
        issued=Coalesce(Sum("amount", filter=Q(amount__gt=0)), 0),
        redeemed=Coalesce(Sum("amount", filter=Q(amount__lt=0)), 0),
    )

    return {
        "range": date_range,
        "sales": {
            "placed": placed["count"],
            "paid": paid_count,
            "gross": gross,
            "discounts": placed["discounts"],
            "aov": aov,
        },
        "pipeline": pipeline,
        "payments": payments,
        "customers": customers,
        "inventory": {**stock, **holds},
        "support": support,
        "reviews": reviews,
        "loop": loop,
        "drops": drops,
        "quests": quests,
        "promotions": promotions,
        "points": {"issued": points["issued"], "redeemed": -points["redeemed"]},
    }


def _broker_kind() -> str:
    """A credential-free label for the configured broker ("redis", "eager", ...)."""
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return "eager (in-process)"
    # The scheme only: the URL itself may carry a password, and this dict is rendered.
    return urlsplit(settings.CELERY_BROKER_URL).scheme or "unknown"


def health_report() -> dict:
    """The system-health screen: live probes, today's telemetry, safe labels.

    Everything here is computed on the page load -- no aggregation over history, and no
    invented numbers: an unavailable measurement reports as unavailable. The environment
    block is deliberately labels only (settings module, DB vendor, cache backend class,
    broker URL scheme), because this screen is projected in ops rooms and must never
    render a hostname or a credential. ``healthy`` collapses the probe results into one
    word the template can lead with instead of re-deriving the rule.
    """
    checks = [
        {"label": label, "status": probe()}
        for label, probe in (
            ("Database", health_checks.check_database),
            ("Cache", health_checks.check_cache),
            ("Celery broker", health_checks.check_broker),
        )
    ]
    snapshot = telemetry.snapshot()
    return {
        "checks": checks,
        "healthy": all(row["status"] != "down" for row in checks),
        "metrics": {
            **snapshot,
            # Templates render ``None`` as the word "None"; carry the question as a bool.
            "queue_depth_measured": snapshot["queue_depth"] is not None,
        },
        "environment": {
            "settings_module": os.environ.get("DJANGO_SETTINGS_MODULE", ""),
            "debug": settings.DEBUG,
            "database_vendor": connection.vendor,
            "cache_backend": settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1],
            "broker": _broker_kind(),
        },
    }
