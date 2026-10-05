"""Catalogue, inventory, drops, promotions and quests (Phase 16 reads).

Merchandising selectors. Stock and products are read here and *written only by their own
services* (:func:`apps.inventory.services.adjust_stock`,
:func:`apps.catalog.services.publish`) -- the back office never sets ``on_hand`` or
``Product.status`` from a form field of its own.
"""

from __future__ import annotations

from django.db.models import Count, F, FloatField, Prefetch, Q, QuerySet
from django.db.models.functions import Cast
from django.utils import timezone

from apps.backoffice.selectors.common import sort_queryset
from apps.catalog.models import Product, ProductVariant
from apps.drops.models import DropStatus, FlashDrop
from apps.engagement.models import Promotion
from apps.inventory.models import InventoryMovement, Stock
from apps.quests.models import Quest

__all__ = [
    "PRODUCT_SORTS",
    "catalog_health",
    "drops",
    "low_stock_rows",
    "products",
    "promotion_state",
    "promotions",
    "quests",
    "recent_movements",
    "stock_rows",
    "stock_state",
]

PRODUCT_SORTS = {
    "-created_at": ["-created_at", "-pk"],
    "created_at": ["created_at", "pk"],
    "name": ["name"],
    "-name": ["-name"],
    "-price": ["-variants__price", "-pk"],
}
STOCK_SORTS = {
    "sku": ["variant__sku"],
    "-sku": ["-variant__sku"],
    "available": ["-variant__product__name", "-variant__sku"],
    "-updated_at": ["-updated_at", "-pk"],
    "on_hand": ["-on_hand", "-variant__sku"],
}


def stock_state(stock: Stock, threshold: int, available: int | None = None) -> str:
    """A word for the row, so the template never computes business state itself.

    ``available`` comes from the ``qty_available`` annotation when the row was listed
    (annotating ``available`` itself would collide with the model's read-only property,
    which Django materialises with ``setattr``). Un-annotated rows fall back to it.
    """
    if available is None:
        available = stock.available
    if available <= 0:
        return "out"
    if available <= threshold:
        return "low"
    if stock.reserved > 0:
        return "reserved"
    return "ok"


def stock_rows(
    *, q: str = "", state: str = "", product: str = "", category: str = "", sort: str = ""
) -> QuerySet:
    """Every SKU with its counters and the product it hangs off.

    ``qty_available`` is annotated rather than filtered through the model property: a
    property cannot appear in a ``WHERE`` clause, and computing it in Python would mean
    loading the table first.
    """
    qs = Stock.objects.select_related(
        "variant__product", "variant__color", "variant__size"
    ).annotate(qty_available=F("on_hand") - F("reserved"))

    needle = (q or "").strip()
    if needle:
        qs = qs.filter(
            Q(variant__sku__icontains=needle) | Q(variant__product__name__icontains=needle)
        )
    if product:
        qs = qs.filter(variant__product__slug=product.strip())
    if category:
        qs = qs.filter(variant__product__category__slug=category.strip())

    threshold = _low_stock_threshold()
    if state == "out":
        qs = qs.filter(qty_available__lte=0)
    elif state == "low":
        qs = qs.filter(qty_available__gt=0, qty_available__lte=threshold)
    elif state == "reserved":
        qs = qs.filter(reserved__gt=0)

    return sort_queryset(qs, sort, STOCK_SORTS, "-updated_at")[0]


def low_stock_rows() -> QuerySet:
    """Variants at or below the low-stock threshold (used by tiles and alerts)."""
    threshold = _low_stock_threshold()
    return Stock.objects.annotate(qty_available=F("on_hand") - F("reserved")).filter(
        qty_available__lte=threshold
    )


def recent_movements(*, limit: int = 40, variant_pk=None) -> QuerySet:
    """The ledger, newest first -- evidence of what changed and who changed it."""
    qs = InventoryMovement.objects.select_related("variant", "variant__product", "user")
    if variant_pk:
        qs = qs.filter(variant__pk=variant_pk)
    return qs.order_by("-created_at", "-pk")[:limit]


def _low_stock_threshold() -> int:
    from django.conf import settings

    return settings.BACKOFFICE_LOW_STOCK_THRESHOLD


def products(*, q: str = "", status: str = "", issue: str = "", sort: str = "") -> QuerySet:
    """Products with the counters the quality flags need, computed in the query.

    ``image_count`` and ``active_variant_count`` are annotations rather than per-row
    counts so the list stays at a fixed number of queries no matter how wide the page is.
    """
    qs = (
        Product.objects.select_related("brand", "category")
        .prefetch_related(Prefetch("images"))
        .annotate(
            image_count=Count("images", distinct=True),
            variant_count=Count("variants", distinct=True),
            active_variant_count=Count(
                "variants", filter=Q(variants__is_active=True), distinct=True
            ),
        )
    )
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(Q(name__icontains=needle) | Q(slug__icontains=needle))
    if status in Product.Status.values:
        qs = qs.filter(status=status)

    if issue == "unpublished":
        qs = qs.filter(~Q(status=Product.Status.ACTIVE))
    elif issue == "missing_images":
        qs = qs.filter(image_count=0)
    elif issue == "no_active_variants":
        qs = qs.filter(active_variant_count=0)

    return sort_queryset(qs, sort, PRODUCT_SORTS, "-created_at")[0]


def catalog_health() -> dict[str, int]:
    """Counts of every catalogue exception the desk cares about -- one query each."""
    active = Product.objects.filter(status=Product.Status.ACTIVE)
    return {
        "products_total": Product.objects.count(),
        "drafts": Product.objects.filter(status=Product.Status.DRAFT).count(),
        "archived": Product.objects.filter(status=Product.Status.ARCHIVED).count(),
        "missing_images": Product.objects.annotate(n=Count("images")).filter(n=0).count(),
        "no_active_variants": Product.objects.annotate(
            n=Count("variants", filter=Q(variants__is_active=True))
        )
        .filter(n=0)
        .count(),
        "active_without_publish_date": active.filter(published_at__isnull=True).count(),
        "inactive_variants": ProductVariant.objects.filter(is_active=False).count(),
    }


def drops(*, status: str = "", q: str = "") -> QuerySet:
    """Drops with their product rows and allocations eager-loaded."""
    qs = FlashDrop.objects.prefetch_related("products__product", "allocations").order_by(
        "-starts_at", "-pk"
    )
    if status in DropStatus.values:
        qs = qs.filter(status=status)
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(Q(name__icontains=needle) | Q(slug__icontains=needle))
    return qs


def promotion_state(promotion: Promotion, now=None) -> str:
    """scheduled / active / expired / inactive -- one place, never re-derived in a view."""
    now = now or timezone.localtime()
    if not promotion.is_active:
        return "inactive"
    if promotion.starts_at and now < promotion.starts_at:
        return "scheduled"
    if promotion.ends_at and now >= promotion.ends_at:
        return "expired"
    return "active"


def promotions(*, scope: str = "", q: str = "") -> QuerySet:
    """Promotions with usage against limit (``used_count`` is the domain's own counter)."""
    qs = Promotion.objects.order_by("-starts_at", "-pk")
    if scope:
        now = timezone.localtime()
        if scope == "active":
            qs = qs.filter(is_active=True, starts_at__lte=now).filter(
                Q(ends_at__isnull=True) | Q(ends_at__gt=now)
            )
        elif scope == "scheduled":
            qs = qs.filter(is_active=True, starts_at__gt=now)
        elif scope == "expired":
            qs = qs.filter(Q(ends_at__lte=now) | Q(is_active=False))
        elif scope == "limit":
            from django.conf import settings

            fraction = settings.BACKOFFICE_PROMOTION_LIMIT_FRACTION
            qs = (
                qs.filter(is_active=True, usage_limit__isnull=False)
                .annotate(
                    used_fraction=Cast("used_count", FloatField()) / F("usage_limit"),
                )
                .filter(used_fraction__gte=fraction)
            )
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(Q(code__icontains=needle) | Q(name__icontains=needle))
    return qs


def quests(*, q: str = "", publish_state: str = "", sort: str = "") -> QuerySet:
    """Quest definitions with participation counts (one aggregate, not per row)."""
    qs = Quest.objects.annotate(
        participant_count=Count("progress", distinct=True),
        completed_count=Count("progress", filter=Q(progress__status="completed"), distinct=True),
    ).order_by("sort_order", "-pk")
    if publish_state in Quest.PublishState.values:
        qs = qs.filter(publish_state=publish_state)
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(Q(name__icontains=needle) | Q(slug__icontains=needle))
    return qs
