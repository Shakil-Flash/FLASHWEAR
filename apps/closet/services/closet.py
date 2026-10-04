"""Wardrobe queries, filtering and ownership rules (Phase 8).

Three jobs, one module:

* **Reads** -- :func:`get_user_closet` / :func:`get_available_closet_items` are the only
  doorways future features (including the AI stylist) use, so eager-loading policy lives in
  one place instead of being rediscovered per page.
* **Filtering** -- :func:`filter_closet` runs entirely in the ORM with validated inputs and
  an allowlisted sort dict. Nothing a customer types ever reaches ``order_by``.
* **Writes** -- manual rows are created here; purchased rows are created *only* from a
  delivered order line the caller owns, with the database's ``(order_item, unit)``
  constraint closing the double-click race. ``source`` is written by this module and is
  ``editable=False`` everywhere else.

No signal ever calls this module: the wardrobe only changes when the customer acts.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, QuerySet

from apps.closet.models import ClosetItem
from apps.closet.services.errors import ClosetError

__all__ = [
    "add_purchased_units",
    "count_by_category",
    "create_manual_item",
    "delete_item",
    "filter_closet",
    "get_available_closet_items",
    "get_user_closet",
    "guess_category",
    "purchase_candidates",
    "remaining_by_order_item",
    "remaining_units",
    "set_status",
]

# Public sort allowlist -> order_by tuple. Never pass a request value to order_by directly.
SORT_CHOICES = {
    "newest": ("-created_at",),
    "oldest": ("created_at",),
    "name_asc": ("name", "-created_at"),
    "name_desc": ("-name", "-created_at"),
}
DEFAULT_SORT = "newest"

# The one status the closet page shows unless the caller asks for something else.
ACTIVE = ClosetItem.Status.ACTIVE
ARCHIVED = ClosetItem.Status.ARCHIVED


def _optimized(queryset: QuerySet) -> QuerySet:
    """Eager-load everything a closet card or outfit row needs, once.

    ``variant__product`` covers the name/brand link, ``variant__product__images`` the borrowed
    catalogue photo, and colour/size the option labels -- without this a 40-item page would
    fire dozens of queries (the query-count tests pin it).
    """
    return queryset.select_related(
        "variant",
        "variant__product",
        "variant__product__brand",
        "variant__color",
        "variant__size",
    ).prefetch_related("variant__product__images")


def get_user_closet(user) -> QuerySet:
    """Every row in one customer's wardrobe, newest first, eager-loaded.

    Scoped by ``user`` here so callers cannot forget it; ownership is never a view-layer
    afterthought.
    """
    return _optimized(ClosetItem.objects.filter(user=user))


def get_available_closet_items(user) -> QuerySet:
    """Active pieces only -- what the outfit builder is allowed to add.

    Archived items stay out of *new* compositions but remain inside old ones (the outfit
    side renders them as archived instead).
    """
    return get_user_closet(user).filter(status=ACTIVE)


def filter_closet(
    user,
    *,
    category: str = "",
    color: str = "",
    brand: str = "",
    source: str = "",
    season: str = "",
    occasion: str = "",
    style: str = "",
    status: str = ACTIVE,
    q: str = "",
    sort: str = DEFAULT_SORT,
) -> QuerySet:
    """Filter and sort one wardrobe in the database.

    Every value arrives already validated by a form/serializer ``ChoiceField`` (or is
    dropped when empty); ``sort`` goes through the allowlist. ``status`` accepts the two
    real states or ``all`` for the archive view.
    """
    queryset = get_user_closet(user)
    if status == "all":
        pass
    elif status in (ACTIVE, ARCHIVED):
        queryset = queryset.filter(status=status)
    else:
        queryset = queryset.filter(status=ACTIVE)

    for field, value in (
        ("category", category),
        ("color", color),
        ("brand", brand),
        ("source", source),
        ("season", season),
        ("occasion", occasion),
        ("style", style),
    ):
        if value:
            queryset = queryset.filter(**{field: value})

    text = (q or "").strip()
    if text:
        queryset = queryset.filter(
            Q(name__icontains=text) | Q(brand__icontains=text) | Q(notes__icontains=text)
        )

    return queryset.order_by(*SORT_CHOICES.get(sort, SORT_CHOICES[DEFAULT_SORT]))


def count_by_category(user) -> dict[str, int]:
    """Active counts per category for the closet's category rail -- one grouped query."""
    rows = (
        ClosetItem.objects.filter(user=user, status=ACTIVE)
        .values("category")
        .annotate(n=Count("id"))
    )
    return {row["category"]: row["n"] for row in rows}


def _validate(item: ClosetItem) -> None:
    """Run model validation, speaking the service's error vocabulary.

    Forms already validate the same rules for their own fields; this is the defensive layer
    for direct service calls (and it is what turns a bad image into a message, not a 500).
    """
    try:
        item.full_clean()
    except ValidationError as exc:
        detail = (
            "; ".join(message for messages in exc.message_dict.values() for message in messages)
            if hasattr(exc, "message_dict")
            else "; ".join(exc.messages)
        )
        raise ClosetError(detail or "That item could not be saved.", code="invalid") from exc


def create_manual_item(user, **fields) -> ClosetItem:
    """Create a manually owned piece. ``variant``/``order_item``/``source`` are not accepted:
    a payload can only ever describe clothing the customer already owns by hand.
    """
    allowed = {
        "category",
        "name",
        "brand",
        "color",
        "size",
        "material",
        "notes",
        "season",
        "occasion",
        "style",
        "image",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ClosetError("Those fields do not belong on a manual item.", code="unexpected_fields")
    item = ClosetItem(user=user, source=ClosetItem.Source.MANUAL, **fields)
    _validate(item)
    item.save()
    return item


def set_status(item: ClosetItem, status: str) -> ClosetItem:
    """Archive or restore a piece. Status transitions never touch outfit history."""
    if status not in (ACTIVE, ARCHIVED):
        raise ClosetError("Unknown closet status.", code="bad_status")
    if item.status != status:
        item.status = status
        item.save(update_fields=["status", "updated_at"])
    return item


def delete_item(item: ClosetItem) -> None:
    """Hard-remove a piece, refusing while an outfit still wears it.

    PROTECT on ``OutfitItem.closet_item`` is the backstop; catching it turns a database
    error into the honest instruction (archive instead) rather than a 500.
    """
    from django.db.models import ProtectedError

    try:
        item.delete()
    except ProtectedError as exc:
        raise ClosetError(
            "This piece is part of an outfit. Archive it instead so the outfit keeps its history.",
            code="in_use",
        ) from exc


def remaining_units(order_item) -> int:
    """How many physical units of an order line are not in the wardrobe yet."""
    owned = ClosetItem.objects.filter(order_item=order_item).count()
    return max(0, order_item.quantity - owned)


def remaining_by_order_item(order) -> dict[int, int]:
    """Remaining units for every line of one order, in two grouped queries.

    The order detail page renders this next to its (already prefetched) lines, so "Add to
    closet" can show how many units are still missing without a query per line.
    """
    owned_counts: dict[int, int] = {}
    owned_ids = ClosetItem.objects.filter(order_item__order=order).values_list(
        "order_item_id", flat=True
    )
    for order_item_id in owned_ids:
        owned_counts[order_item_id] = owned_counts.get(order_item_id, 0) + 1
    return {
        item.pk: max(0, item.quantity - owned_counts.get(item.pk, 0)) for item in order.items.all()
    }


def purchase_candidates(user) -> list[tuple]:
    """Delivered order lines with units still missing from the wardrobe.

    The closet's "add from purchases" page reads this: newest orders first, already-full
    lines dropped (the POST endpoint reports those as already added). Each line carries its
    remaining count and the deterministic category prefill.
    """
    from apps.orders.models import Order

    rows: list[tuple] = []
    orders = Order.objects.filter(user=user, status=Order.Status.DELIVERED).prefetch_related(
        "items__variant__product"
    )
    for order in orders:
        remaining = remaining_by_order_item(order)
        for item in order.items.all():
            left = remaining.get(item.pk, 0)
            if left > 0:
                rows.append((order, item, left, guess_category(item.variant.product)))
    return rows


# Best-effort category guess for the purchase-add prefill. The customer confirms (or
# changes) the choice in the form; this only saves a dropdown interaction. Checked in a
# fixed order so a "shirt-dress" lands in Dresses rather than Tops.
CATEGORY_GUESS_TERMS = (
    (ClosetItem.Category.SHOES, ("shoe", "trainer", "sneaker", "boot", "footwear")),
    (ClosetItem.Category.DRESSES, ("dress", "jumpsuit", "romper")),
    (ClosetItem.Category.BOTTOMS, ("bottom", "jean", "trouser", "short", "skirt", "chino")),
    (ClosetItem.Category.OUTERWEAR, ("outer", "jacket", "coat", "parka", "blazer")),
    (
        ClosetItem.Category.ACCESSORIES,
        (
            "accessor",
            "hat",
            "cap",
            "bag",
            "belt",
            "sock",
        ),
    ),
    (
        ClosetItem.Category.TOPS,
        (
            "top",
            "tee",
            "t-shirt",
            "shirt",
            "tank",
            "sweater",
            "knit",
            "jumper",
            "polo",
        ),
    ),
)


def guess_category(product) -> str:
    """Prefill a wardrobe category from the product's catalogue taxonomy (deterministic)."""
    parts = [product.name or ""]
    if product.category is not None:
        parts.extend([product.category.name, product.category.slug])
    haystack = " ".join(parts).lower()
    for category, terms in CATEGORY_GUESS_TERMS:
        if any(term in haystack for term in terms):
            return category
    # Walk up the category tree: parents like "Tops" carry the wardrobe word.
    node = product.category
    while node is not None:
        label = f"{node.name} {node.slug}".lower()
        for category, terms in CATEGORY_GUESS_TERMS:
            if any(term in label for term in terms):
                return category
        node = node.parent
    return ClosetItem.Category.TOPS


def add_purchased_units(
    user, order_item, *, units: int = 1, category: str | None = None
) -> list[ClosetItem]:
    """Move ``units`` physical units of a delivered order line into the wardrobe.

    Server-derived eligibility, checked here rather than in a view: the line must belong to
    ``user`` and its order must be **delivered** -- the ownership event Phase 8 chose (see
    the README). ``source`` is stamped by us; the caller never supplies it.

    ``category`` is the customer's confirmed wardrobe category (validated upstream); it
    defaults to the deterministic :func:`guess_category` prefill.

    Idempotent by construction: owned units are counted first, the request is clamped to the
    purchased quantity, and the ``(order_item, unit)`` unique constraint closes the race
    where two requests pick the same next unit. A repeat click on a fully-added line adds
    nothing and reports zero created rows.
    """
    if order_item.order.user_id != user.id:
        raise ClosetError("That order line is not yours.", code="not_owner")
    if order_item.order.status != order_item.order.Status.DELIVERED:
        raise ClosetError(
            "Only delivered orders can be added to your closet.", code="not_delivered"
        )
    if units < 1:
        raise ClosetError("Add at least one unit.", code="bad_units")

    created: list[ClosetItem] = []
    with transaction.atomic():
        existing = ClosetItem.objects.filter(order_item=order_item).count()
        available = max(0, order_item.quantity - existing)
        for offset in range(min(units, available)):
            try:
                created.append(
                    _insert_purchased_unit(
                        user,
                        order_item,
                        unit=existing + offset + 1,
                        category=category,
                    )
                )
            except (IntegrityError, ValidationError):
                # Lost the race for this unit (or a constraint re-checked mid-flight):
                # someone already claimed it. Stop; no duplicate row can exist.
                break
    return created


def _insert_purchased_unit(user, order_item, *, unit: int, category: str | None) -> ClosetItem:
    """Snapshot one unit's catalogue metadata into wardrobe columns and insert it."""
    variant = order_item.variant
    product = variant.product
    materials = ", ".join(material.name for material in product.materials.all())
    item = ClosetItem(
        user=user,
        source=ClosetItem.Source.PURCHASED,
        variant=variant,
        order_item=order_item,
        unit=unit,
        category=category or guess_category(product),
        name=product.name,
        brand=product.brand.name if product.brand else "",
        color=variant.color.name if variant.color else "",
        size=variant.size.name if variant.size else "",
        material=materials,
    )
    item.full_clean()
    item.save()
    return item
