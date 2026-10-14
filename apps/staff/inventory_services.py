"""Inventory & Stock Operations service layer for FLASHWEAR Staff Portal (Phase 62).

Authoritative source of stock queries, safe adjustments, receiving, and ledger auditing.
Directs all mutations strictly through apps.inventory.services.adjust_stock to preserve
transactional atomicity, row-level locking, and append-only ledger invariants.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.backoffice.services.audit import record as record_audit_event
from apps.catalog.models import Brand, Category, ProductVariant
from apps.inventory.models import InventoryMovement, Reservation, Stock
from apps.inventory.services import InsufficientStock, adjust_stock
from apps.staff.constants import (
    INVENTORY_MANAGE,
    INVENTORY_VIEW,
    StaffRole,
)
from apps.staff.dashboard_services import parse_date_range
from apps.staff.permissions import get_user_roles, has_staff_capability

logger = logging.getLogger("flashwear.staff.inventory")

PAGE_SIZE = 25
LEDGER_PAGE_SIZE = 30


# =============================================================================
# Role & Authority Helpers
# =============================================================================


def is_inventory_manager(user: Any) -> bool:
    """Return True if user has managerial oversight (Owner, Operations Manager, or Superuser)."""
    if getattr(user, "is_superuser", False):
        return True
    roles = get_user_roles(user)
    return any(r in (StaffRole.OWNER, StaffRole.OPERATIONS_MANAGER) for r in roles)


def can_manage_inventory(user: Any) -> bool:
    """Return True if user can perform stock adjustments or receipts."""
    return has_staff_capability(user, INVENTORY_MANAGE)


# =============================================================================
# 1. Workspace Inventory Query & Aggregations
# =============================================================================


def get_filtered_inventory(params: dict[str, Any]) -> dict[str, Any]:
    """Searchable, paginated inventory query with multi-attribute filtering and metrics."""
    default_threshold = getattr(settings, "BACKOFFICE_LOW_STOCK_THRESHOLD", 5)

    qs = Stock.objects.select_related(
        "variant__product__category",
        "variant__product__brand",
        "variant__color",
        "variant__size",
    ).order_by("variant__product__name", "variant__color__name", "variant__size__name")

    # 1. Search Query
    search_query = params.get("q", "").strip()
    if search_query:
        qs = qs.filter(
            Q(variant__sku__icontains=search_query)
            | Q(variant__barcode__icontains=search_query)
            | Q(variant__product__name__icontains=search_query)
            | Q(variant__color__name__icontains=search_query)
            | Q(variant__size__name__icontains=search_query)
        )

    # 2. Stock Status Filter
    status_filter = params.get("status", "all").strip().lower()
    if status_filter == "out_of_stock":
        qs = qs.filter(on_hand__lte=0)
    elif status_filter == "low_stock":
        qs = qs.filter(
            Q(on_hand__gt=0, on_hand__lte=F("low_stock_threshold"))
            | (
                Q(low_stock_threshold__isnull=True)
                & Q(on_hand__gt=0, on_hand__lte=default_threshold)
            )
        )
    elif status_filter == "in_stock":
        qs = qs.filter(
            Q(low_stock_threshold__isnull=False, on_hand__gt=F("low_stock_threshold"))
            | (Q(low_stock_threshold__isnull=True) & Q(on_hand__gt=default_threshold))
        )

    # 3. Category Filter
    category_slug = params.get("category", "").strip()
    if category_slug:
        qs = qs.filter(variant__product__category__slug=category_slug)

    # 4. Brand Filter
    brand_slug = params.get("brand", "").strip()
    if brand_slug:
        qs = qs.filter(variant__product__brand__slug=brand_slug)

    # 5. Global Metric Aggregations
    total_skus = Stock.objects.count()
    aggregates = Stock.objects.aggregate(
        total_on_hand=Sum("on_hand"),
        total_reserved=Sum("reserved"),
    )
    total_on_hand = aggregates["total_on_hand"] or 0
    total_reserved = aggregates["total_reserved"] or 0
    total_available = max(0, total_on_hand - total_reserved)

    out_of_stock_count = Stock.objects.filter(on_hand__lte=0).count()
    low_stock_count = Stock.objects.filter(
        Q(on_hand__gt=0, on_hand__lte=F("low_stock_threshold"))
        | (Q(low_stock_threshold__isnull=True) & Q(on_hand__gt=0, on_hand__lte=default_threshold))
    ).count()

    # 6. Sorting
    sort_by = params.get("sort", "").strip().lower()
    if sort_by == "on_hand_asc":
        qs = qs.order_by("on_hand", "variant__sku")
    elif sort_by == "on_hand_desc":
        qs = qs.order_by("-on_hand", "variant__sku")
    elif sort_by == "sku":
        qs = qs.order_by("variant__sku")
    elif sort_by == "product":
        qs = qs.order_by("variant__product__name", "variant__sku")

    # 7. Pagination
    paginator = Paginator(qs, PAGE_SIZE)
    page_number = params.get("page", 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    categories = Category.objects.all().order_by("name")
    brands = Brand.objects.all().order_by("name")
    in_stock_count = max(0, total_skus - low_stock_count - out_of_stock_count)

    kpis_data = {
        "total_skus": total_skus,
        "total_on_hand": total_on_hand,
        "total_reserved": total_reserved,
        "total_available": total_available,
        "in_stock_count": in_stock_count,
        "low_stock_count": low_stock_count,
        "out_of_stock_count": out_of_stock_count,
    }

    return {
        "page_obj": page_obj,
        "paginator": paginator,
        "stocks": page_obj.object_list,
        "search_query": search_query,
        "status_filter": status_filter,
        "category_slug": category_slug,
        "category_filter": category_slug,
        "brand_slug": brand_slug,
        "brand_filter": brand_slug,
        "sort_by": sort_by,
        "categories": categories,
        "brands": brands,
        "metrics": kpis_data,
        "kpis": kpis_data,
    }


# =============================================================================
# 2. Variant Stock Detail
# =============================================================================


def get_variant_stock_details(variant_id: int, user: Any) -> dict[str, Any]:
    """Retrieve full stock inspection context for a single product variant."""
    if not has_staff_capability(user, INVENTORY_VIEW):
        raise PermissionDenied(_("You do not have permission to view inventory."))

    try:
        variant = ProductVariant.objects.select_related(
            "product__category",
            "product__brand",
            "color",
            "size",
        ).get(pk=variant_id)
    except ProductVariant.DoesNotExist as err:
        raise ValidationError(_("Product variant not found.")) from err

    stock = Stock.get_for_variant(variant)

    # Recent movements for this SKU
    recent_movements = variant.inventory_movements.select_related("user").order_by("-created_at")[
        :25
    ]

    # Active reservations
    active_holds = (
        variant.reservations.filter(status=Reservation.Status.ACTIVE)
        .select_related("checkout", "order")
        .order_by("-created_at")[:10]
    )

    return {
        "variant": variant,
        "stock": stock,
        "recent_movements": recent_movements,
        "active_holds": active_holds,
        "can_manage": can_manage_inventory(user),
        "is_manager": is_inventory_manager(user),
    }


# =============================================================================
# 3. Safe Stock Adjustments & Receiving
# =============================================================================


VALID_ADJUSTMENT_TYPES = {
    "received": {
        "label": _("Stock Receipt (Supplier Delivery)"),
        "kind": InventoryMovement.Kind.RECEIVED,
        "sign": 1,
    },
    "correction_add": {
        "label": _("Manual Correction (+ Count Discrepancy)"),
        "kind": InventoryMovement.Kind.ADJUSTMENT,
        "sign": 1,
    },
    "correction_sub": {
        "label": _("Manual Correction (- Count Discrepancy)"),
        "kind": InventoryMovement.Kind.ADJUSTMENT,
        "sign": -1,
    },
    "damaged": {
        "label": _("Damaged / Unsellable Write-Off"),
        "kind": InventoryMovement.Kind.DAMAGED,
        "sign": -1,
    },
    "loss": {
        "label": _("Loss / Shrinkage Write-Off"),
        "kind": InventoryMovement.Kind.LOSS,
        "sign": -1,
    },
}


@transaction.atomic
def record_stock_adjustment(
    variant_id: int,
    *,
    adjustment_type: str,
    quantity: int,
    reason: str,
    actor: Any,
    reference: str = "",
    note: str = "",
    idempotency_token: str = "",
) -> tuple[Stock, InventoryMovement]:
    """Validate and execute a stock adjustment via the authoritative adjust_stock service."""
    if not can_manage_inventory(actor):
        raise PermissionDenied(_("You do not have permission to adjust inventory."))

    if adjustment_type not in VALID_ADJUSTMENT_TYPES:
        raise ValidationError(_("Invalid adjustment type '%(type)s'.") % {"type": adjustment_type})

    if not isinstance(quantity, int) or quantity <= 0:
        raise ValidationError(_("Quantity must be a positive whole number greater than zero."))

    reason_clean = reason.strip()
    if not reason_clean:
        raise ValidationError(_("A reason is mandatory for all manual stock adjustments."))

    try:
        variant = ProductVariant.objects.get(pk=variant_id)
    except ProductVariant.DoesNotExist as err:
        raise ValidationError(_("Product variant not found.")) from err

    # Idempotency / Duplicate Submission Guard
    clean_token = idempotency_token.strip()
    if clean_token:
        recent_cutoff = timezone.now() - timedelta(minutes=5)
        existing_dup = InventoryMovement.objects.filter(
            variant=variant,
            reference=clean_token,
            created_at__gte=recent_cutoff,
        ).first()
        if existing_dup:
            logger.warning(
                "Duplicate inventory submission detected with token '%s' for variant %s",
                clean_token,
                variant.sku,
            )
            stock = Stock.get_for_variant(variant)
            return stock, existing_dup

    type_meta = VALID_ADJUSTMENT_TYPES[adjustment_type]
    delta = quantity * type_meta["sign"]
    kind = type_meta["kind"]

    ref_to_save = clean_token or reference.strip() or "manual"
    full_note = f"{reason_clean} ({note.strip()})" if note.strip() else reason_clean

    try:
        stock = adjust_stock(
            variant=variant,
            delta=delta,
            kind=kind,
            user=actor,
            note=full_note,
            reference=ref_to_save,
        )
    except InsufficientStock as err:
        raise ValidationError(
            _("Cannot adjust %(sku)s by %(delta)d: insufficient stock on hand.")
            % {"sku": variant.sku, "delta": delta}
        ) from err
    except ValueError as err:
        raise ValidationError(str(err)) from err

    # Retrieve the ledger entry created by adjust_stock
    movement = (
        InventoryMovement.objects.filter(variant=variant, user=actor)
        .order_by("-created_at")
        .first()
    )

    action_name = (
        "inventory.received" if kind == InventoryMovement.Kind.RECEIVED else "inventory.adjust"
    )
    record_audit_event(
        actor=actor,
        domain="inventory",
        action=action_name,
        object_type="product_variant",
        object_id=str(variant.pk),
        object_repr=f"Variant {variant.sku}",
        reason=full_note,
        metadata={
            "sku": variant.sku,
            "adjustment_type": adjustment_type,
            "quantity": quantity,
            "delta": delta,
            "on_hand_after": stock.on_hand,
            "available_after": stock.available,
            "reference": ref_to_save,
        },
    )

    logger.info(
        "Stock adjusted for %s: delta=%+d, kind=%s by actor=%s",
        variant.sku,
        delta,
        kind,
        actor,
    )
    return stock, movement


# =============================================================================
# 4. Compensating Reversals
# =============================================================================


@transaction.atomic
def record_compensating_reversal(
    movement_id: int,
    *,
    reason: str,
    actor: Any,
) -> tuple[Stock, InventoryMovement]:
    """Reverse an existing manual adjustment by creating an exact compensating movement."""
    if not can_manage_inventory(actor):
        raise PermissionDenied(_("You do not have permission to reverse inventory movements."))

    reason_clean = reason.strip()
    if not reason_clean:
        raise ValidationError(_("A reason is mandatory when reversing an inventory movement."))

    try:
        original = InventoryMovement.objects.select_related("variant").get(pk=movement_id)
    except InventoryMovement.DoesNotExist as err:
        raise ValidationError(
            _("Inventory movement #%(id)d not found.") % {"id": movement_id}
        ) from err

    # Reversals are allowed on manual movements: RECEIVED, ADJUSTMENT, DAMAGED, LOSS
    reversible_kinds = (
        InventoryMovement.Kind.RECEIVED,
        InventoryMovement.Kind.ADJUSTMENT,
        InventoryMovement.Kind.DAMAGED,
        InventoryMovement.Kind.LOSS,
    )
    if original.kind not in reversible_kinds:
        raise ValidationError(
            _(
                "Cannot reverse movement kind '%(kind)s'. Only manual adjustments, receipts, "
                "damaged items, and stock loss can be reversed."
            )
            % {"kind": original.get_kind_display()}
        )

    # Check if already reversed
    reversal_ref = f"reversal:{original.pk}"
    already_reversed = InventoryMovement.objects.filter(reference=reversal_ref).exists()
    if already_reversed:
        raise ValidationError(
            _("Movement #%(id)d has already been reversed by a compensating movement.")
            % {"id": original.pk}
        )

    compensating_delta = -original.on_hand_delta
    if compensating_delta == 0:
        raise ValidationError(_("This movement has an on-hand change of 0 and cannot be reversed."))

    full_note = _("Compensating reversal of movement #%(id)d: %(reason)s") % {
        "id": original.pk,
        "reason": reason_clean,
    }

    try:
        stock = adjust_stock(
            variant=original.variant,
            delta=compensating_delta,
            kind=InventoryMovement.Kind.REVERSAL,
            user=actor,
            note=full_note,
            reference=reversal_ref,
        )
    except InsufficientStock as err:
        raise ValidationError(
            _(
                "Cannot reverse movement: reversing would cause on-hand stock "
                "to dip below held units."
            )
        ) from err

    reversal_movement = (
        InventoryMovement.objects.filter(
            variant=original.variant,
            reference=reversal_ref,
        )
        .order_by("-created_at")
        .first()
    )

    record_audit_event(
        actor=actor,
        domain="inventory",
        action="inventory.reversal",
        object_type="product_variant",
        object_id=str(original.variant.pk),
        object_repr=f"Variant {original.variant.sku}",
        reason=full_note,
        metadata={
            "reversed_movement_id": original.pk,
            "original_delta": original.on_hand_delta,
            "compensating_delta": compensating_delta,
            "sku": original.variant.sku,
            "on_hand_after": stock.on_hand,
        },
    )

    logger.info(
        "Compensating reversal applied for movement #%d on SKU %s: delta=%+d by %s",
        original.pk,
        original.variant.sku,
        compensating_delta,
        actor,
    )
    return stock, reversal_movement


# =============================================================================
# 5. Threshold Configuration
# =============================================================================


@transaction.atomic
def update_reorder_threshold(
    variant_id: int,
    *,
    threshold: int | None,
    actor: Any,
) -> Stock:
    """Update variant-specific reorder threshold."""
    if not can_manage_inventory(actor):
        raise PermissionDenied(_("You do not have permission to configure reorder thresholds."))

    try:
        variant = ProductVariant.objects.get(pk=variant_id)
    except ProductVariant.DoesNotExist as err:
        raise ValidationError(_("Product variant not found.")) from err

    if threshold is not None and (not isinstance(threshold, int) or threshold < 0):
        raise ValidationError(_("Reorder threshold must be zero or a positive integer."))

    stock = Stock.get_for_variant(variant)
    old_threshold = stock.low_stock_threshold
    stock.low_stock_threshold = threshold
    stock.save(update_fields=["low_stock_threshold", "updated_at"])

    record_audit_event(
        actor=actor,
        domain="inventory",
        action="inventory.threshold_updated",
        object_type="product_variant",
        object_id=str(variant.pk),
        object_repr=f"Variant {variant.sku}",
        reason=f"Threshold updated from {old_threshold} to {threshold}",
        metadata={
            "sku": variant.sku,
            "old_threshold": old_threshold,
            "new_threshold": threshold,
        },
    )

    logger.info(
        "Reorder threshold updated for %s: %s -> %s by %s",
        variant.sku,
        old_threshold,
        threshold,
        actor,
    )
    return stock


# =============================================================================
# 6. Movement Ledger Queries
# =============================================================================


def get_stock_ledger_history(params: dict[str, Any], user: Any) -> dict[str, Any]:
    """Retrieve filtered, paginated chronological stock movement ledger."""
    if not has_staff_capability(user, INVENTORY_VIEW):
        raise PermissionDenied(_("You do not have permission to view the inventory ledger."))

    qs = InventoryMovement.objects.select_related(
        "variant__product",
        "variant__color",
        "variant__size",
        "user",
    ).order_by("-created_at", "-pk")

    # 1. Search Query
    search_query = params.get("q", "").strip()
    if search_query:
        qs = qs.filter(
            Q(variant__sku__icontains=search_query)
            | Q(variant__product__name__icontains=search_query)
            | Q(reference__icontains=search_query)
            | Q(note__icontains=search_query)
        )

    # 2. Movement Kind Filter
    kind_filter = params.get("kind", "").strip().lower()
    if kind_filter and kind_filter != "all":
        qs = qs.filter(kind=kind_filter)

    # 3. Specific Variant Filter
    variant_id = params.get("variant_id", "").strip()
    if variant_id.isdigit():
        qs = qs.filter(variant_id=int(variant_id))

    # 4. Date Range Filter
    date_filter = params.get("date_range", "30d").strip()
    start_date, end_date = parse_date_range(date_filter)
    if start_date:
        qs = qs.filter(created_at__gte=start_date)
    if end_date:
        qs = qs.filter(created_at__lte=end_date)

    # 5. Pagination
    paginator = Paginator(qs, LEDGER_PAGE_SIZE)
    page_number = params.get("page", 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return {
        "page_obj": page_obj,
        "paginator": paginator,
        "movements": page_obj.object_list,
        "search_query": search_query,
        "kind_filter": kind_filter,
        "date_filter": date_filter,
        "variant_id": variant_id,
        "kind_choices": InventoryMovement.Kind.choices,
        "can_manage": can_manage_inventory(user),
    }
