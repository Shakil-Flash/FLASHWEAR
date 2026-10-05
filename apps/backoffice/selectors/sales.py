"""Sales-side selectors: orders, payments, shipments, customers, FLASH Points.

Everything here is read-only. Order state is changed by :mod:`apps.orders.services`,
payment state by :mod:`apps.payments.services` -- a selector never writes, so a screen
that forgets a permission check still cannot move money or a parcel.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import (
    Count,
    DecimalField,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
)
from django.utils.translation import gettext_lazy as _

from apps.backoffice.selectors.common import DateRange, sort_queryset
from apps.engagement.models import PointsTransaction
from apps.orders.models import Order, Shipment, ShipmentEvent
from apps.payments.models import Payment
from apps.support.models import SupportTicket

__all__ = [
    "ORDER_SORTS",
    "customer_search",
    "order_detail",
    "order_timeline",
    "orders",
    "payment_detail",
    "payment_events",
    "payments",
    "points_ledger",
    "shipments",
]

#: URL-safe sort key -> ORM ordering. Never built from user input.
ORDER_SORTS = {
    "-created_at": ["-created_at", "-pk"],
    "created_at": ["created_at", "pk"],
    "-total": ["-total", "-pk"],
    "total": ["total", "pk"],
    "number": ["number"],
    "-number": ["-number"],
    "customer": ["user__email", "-pk"],
}

CUSTOMER_SORTS = {
    "email": ["email"],
    "-email": ["-email"],
    "joined": ["date_joined"],
    "-joined": ["-date_joined"],
    "-spend": ["-spend", "-pk"],
    "-orders": ["-order_count", "-pk"],
}

SHIPMENT_SORTS = {"-created_at": ["-created_at", "-pk"], "created_at": ["created_at", "pk"]}
PAYMENT_SORTS = {"-created_at": ["-created_at", "-pk"], "created_at": ["created_at", "pk"]}
LEDGER_SORTS = {"-created_at": ["-created_at", "-pk"], "created_at": ["created_at", "pk"]}


def _clean_choice(value: str | None, allowed) -> str:
    """Keep a value only when it is one of the domain's own choices."""
    return value if value in allowed else ""


def orders(
    *,
    q: str = "",
    status: str = "",
    payment_status: str = "",
    shipment_status: str = "",
    date_range: DateRange | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    product: str = "",
    drop: str = "",
    promotion: str = "",
    sort: str = "",
) -> QuerySet:
    """The order queue: filtered, joined once, ordered by an allowlisted key.

    Filters compose (they are applied in sequence), and a filter that does not recognise
    its value is dropped rather than honoured literally -- ``?status=nonsense`` shows the
    whole queue instead of an empty one that looks like an incident.
    """
    qs = Order.objects.select_related("user", "shipping_address").prefetch_related(
        "items__variant__product", "shipments"
    )
    # The payment is a OneToOne, so its status is a safe (non-multiplying) join filter.
    qs = qs.select_related("payment")

    needle = (q or "").strip()
    if needle:
        qs = qs.filter(Q(number__icontains=needle) | Q(user__email__icontains=needle))
    status = _clean_choice(status, Order.Status.values)
    if status:
        qs = qs.filter(status=status)
    payment_status = _clean_choice(payment_status, Payment.Status.values)
    if payment_status:
        qs = qs.filter(payment__status=payment_status)
    if shipment_status in Shipment.Status.values:
        qs = qs.filter(shipments__status=shipment_status).distinct()
    if date_range is not None and not date_range.is_all_time:
        qs = qs.filter(**date_range.as_query)
    if min_amount is not None:
        qs = qs.filter(total__gte=min_amount)
    if max_amount is not None:
        qs = qs.filter(total__lte=max_amount)
    if product:
        qs = qs.filter(items__variant__product__name__icontains=product.strip()).distinct()
    if drop:
        # Orders carry no drop column; a drop is known through the products it featured.
        qs = qs.filter(items__variant__product__drop_products__drop__slug=drop.strip()).distinct()
    if promotion:
        qs = qs.filter(promotion_code__icontains=promotion.strip())
    return sort_queryset(qs, sort, ORDER_SORTS, "-created_at")[0]


def order_detail(number: str) -> QuerySet:
    """One order, with everything the detail screen dereferences, as a queryset.

    Returned as a queryset so the view can still 404 on a number that belongs to another
    tenant-free but unknown row, and so the template's joins stay eager.
    """
    return (
        Order.objects.filter(number=number)
        .select_related("user", "shipping_address", "payment", "checkout")
        .prefetch_related(
            "items__variant__product",
            "shipments",
            "events__actor",
            "reservations__variant",
        )
    )


def provider_label(payment: Payment) -> str:
    """Human name for the provider, falling back to the raw code if it has no choices."""
    getter = getattr(payment, "get_provider_display", None)
    return str(getter() if callable(getter) else payment.provider)


def order_timeline(order: Order) -> list[dict]:
    """The authoritative history of an order, merged from the three event tables.

    Order, payment and shipment events are the only things that happened -- there is no
    invented "checkout created" step. An order with no rows in any of them renders an
    empty timeline, which is the honest answer.
    """
    entries: list[dict] = []

    for event in order.events.all():
        entries.append(
            {
                "at": event.created_at,
                "source": _("Order"),
                "label": event.get_event_type_display(),
                "note": event.note,
                "actor": getattr(event.actor, "email", ""),
                "metadata": event.metadata or {},
            }
        )

    payment = Payment.objects.filter(order=order).prefetch_related("events").first()
    if payment is not None:
        for event in payment.events.all():
            entries.append(
                {
                    "at": event.received_at,
                    "source": _("Payment"),
                    "label": event.get_event_type_display(),
                    "note": "",
                    "actor": provider_label(payment),
                    "metadata": {},
                }
            )

    for event in ShipmentEvent.objects.filter(shipment__order=order).select_related("actor"):
        entries.append(
            {
                "at": event.created_at,
                "source": _("Shipment"),
                "label": event.get_event_type_display(),
                "note": event.note,
                "actor": getattr(event.actor, "email", ""),
                "metadata": event.metadata or {},
            }
        )

    entries.sort(key=lambda item: item["at"])
    return entries


def payments(
    *,
    q: str = "",
    status: str = "",
    provider: str = "",
    date_range: DateRange | None = None,
    sort: str = "",
) -> QuerySet:
    """Finance queue: provider, amount, state, reference -- never the payload."""
    qs = Payment.objects.select_related("order", "order__user")
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(Q(order__number__icontains=needle) | Q(provider_reference__icontains=needle))
    status = _clean_choice(status, Payment.Status.values)
    if status:
        qs = qs.filter(status=status)
    if provider:
        qs = qs.filter(provider__iexact=provider.strip())
    if date_range is not None and not date_range.is_all_time:
        qs = qs.filter(**date_range.as_query)
    return sort_queryset(qs, sort, PAYMENT_SORTS, "-created_at")[0]


def payment_detail(pk) -> QuerySet:
    return Payment.objects.filter(pk=pk).select_related("order", "order__user")


def payment_events(payment: Payment) -> QuerySet:
    """Provider events, minus their payloads: the payload is the provider's business."""
    return payment.events.all()


def shipments(
    *,
    q: str = "",
    status: str = "",
    carrier: str = "",
    date_range: DateRange | None = None,
    sort: str = "",
) -> QuerySet:
    """Fulfilment queue with the order (and therefore the customer) joined in."""
    qs = Shipment.objects.select_related("order", "order__user")
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(Q(tracking_number__icontains=needle) | Q(order__number__icontains=needle))
    status = _clean_choice(status, Shipment.Status.values)
    if status:
        qs = qs.filter(status=status)
    if carrier:
        qs = qs.filter(carrier__icontains=carrier.strip())
    if date_range is not None and not date_range.is_all_time:
        qs = qs.filter(**date_range.as_query)
    return sort_queryset(qs, sort, SHIPMENT_SORTS, "-created_at")[0]


def customer_search(
    *, q: str = "", order: str = "", date_range: DateRange | None = None, sort: str = ""
) -> QuerySet:
    """Customer search with per-row counters as subqueries.

    Subqueries rather than joins: joining orders *and* tickets onto a user multiplies the
    rows and quietly corrupts any aggregate that is not a ``distinct`` count. The three
    counters are what an operator needs to decide whether an account is worth opening --
    order count, spend (excluding voided orders) and open tickets.
    """
    User = get_user_model()

    orders_for_user = Order.objects.filter(user=OuterRef("pk"))
    order_count = (
        orders_for_user.order_by().values("user").annotate(total=Count("pk")).values("total")
    )
    spend = (
        orders_for_user.exclude(status=Order.Status.CANCELLED)
        .order_by()
        .values("user")
        .annotate(total=Sum("total"))
        .values("total")
    )
    open_tickets = (
        SupportTicket.objects.filter(customer=OuterRef("pk"))
        .exclude(status__in=(SupportTicket.Status.RESOLVED, SupportTicket.Status.CLOSED))
        .order_by()
        .values("customer")
        .annotate(total=Count("pk"))
        .values("total")
    )

    qs = User.objects.annotate(
        order_count=Subquery(order_count, output_field=IntegerField()),
        spend=Subquery(spend, output_field=DecimalField(max_digits=12, decimal_places=2)),
        open_ticket_count=Subquery(open_tickets, output_field=IntegerField()),
    )

    needle = (q or "").strip()
    if needle:
        qs = qs.filter(
            Q(email__icontains=needle)
            | Q(first_name__icontains=needle)
            | Q(last_name__icontains=needle)
        )
    if order:
        qs = qs.filter(orders__number__icontains=order.strip()).distinct()
    if date_range is not None and not date_range.is_all_time:
        qs = qs.filter(date_joined__range=(date_range.start, date_range.end))
    qs = qs.order_by("-date_joined")
    return sort_queryset(qs, sort, CUSTOMER_SORTS, "-joined")[0]


def points_ledger(
    *, q: str = "", transaction_type: str = "", date_range: DateRange | None = None
) -> QuerySet:
    """FLASH Points ledger rows (append-only by construction)."""
    qs = PointsTransaction.objects.select_related("user", "order")
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(Q(user__email__icontains=needle) | Q(reference__icontains=needle))
    if transaction_type in PointsTransaction.TransactionType.values:
        qs = qs.filter(transaction_type=transaction_type)
    if date_range is not None and not date_range.is_all_time:
        qs = qs.filter(**date_range.as_query)
    return qs.order_by("-created_at", "-pk")


def order_events(order: Order) -> QuerySet:
    """Order timeline rows only (used where the other sources are not wanted)."""
    return order.events.select_related("actor")
