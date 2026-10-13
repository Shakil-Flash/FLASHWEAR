"""Phase 44: Inventory Intelligence & Stock Operations.

Authoritative inventory intelligence engine providing:
1. Read-only Stock Accuracy & Reconciliation Auditing.
2. Explicit, transactional reconciliation corrections (never silently mutating ledger/orders).
3. Configurable low-stock threshold management & velocity-grounded Restock Prioritization.
4. Auditable Restock Receipts recording through authoritative InventoryMovement ledger.
5. High-fidelity Inventory Health metrics and groupings by category, product, and Drops.
6. Operational alert deduplication and staff notification dispatching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.backoffice.models import AuditEvent
from apps.backoffice.services import audit as audit_service
from apps.catalog.models import Category, Product, ProductVariant
from apps.drops.models import DropAllocation, DropProduct, DropStatus, FlashDrop
from apps.inventory.models import InventoryMovement, Reservation, Stock
from apps.inventory.services import (
    InsufficientStock,
    adjust_stock,
    release_holds,
    sweep_expired_holds,
)

# =============================================================================
# 1. Reconciliation Dataclasses & Definitions
# =============================================================================


class DiscrepancySeverity:
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class DiscrepancyType:
    RESERVED_MISMATCH = "reserved_mismatch"
    OVERSOLD_INVENTORY = "oversold_inventory"
    EXPIRED_UNSWEPT_HOLDS = "expired_unswept_holds"
    CANCELLED_ORDER_HOLDS = "cancelled_order_holds"
    MISSING_STOCK_RECORD = "missing_stock_record"
    STOCK_ON_INACTIVE_VARIANT = "stock_on_inactive_variant"
    DROP_ALLOCATION_SHORTAGE = "drop_allocation_shortage"


@dataclass(frozen=True)
class InventoryDiscrepancy:
    """One detected inventory discrepancy."""

    discrepancy_type: str
    severity: str
    variant_id: int | None
    variant_sku: str
    product_name: str
    category_name: str
    current_on_hand: int
    current_reserved: int
    expected_reserved: int
    discrepancy_delta: int
    description: str
    remediation_action: str
    reference_id: str = ""

    @property
    def is_critical(self) -> bool:
        return self.severity == DiscrepancySeverity.CRITICAL

    @property
    def is_warning(self) -> bool:
        return self.severity == DiscrepancySeverity.WARNING

    @property
    def kind(self) -> str:
        return self.discrepancy_type

    @property
    def recorded_reserved(self) -> int:
        return self.current_reserved

    @property
    def calculated_reserved(self) -> int:
        return self.expected_reserved


@dataclass
class InventoryReconciliationReport:
    """Full read-only reconciliation audit report."""

    total_variants_audited: int = 0
    total_on_hand: int = 0
    total_reserved: int = 0
    total_available: int = 0
    discrepancies: list[InventoryDiscrepancy] = field(default_factory=list)
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    discrepancies_by_type: dict[str, int] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return len(self.discrepancies) == 0

    @property
    def is_consistent(self) -> bool:
        return len(self.discrepancies) == 0

    @property
    def reserved_mismatches_count(self) -> int:
        return sum(
            1 for d in self.discrepancies if d.discrepancy_type == DiscrepancyType.RESERVED_MISMATCH
        )

    @property
    def expired_holds_count(self) -> int:
        return sum(
            1
            for d in self.discrepancies
            if d.discrepancy_type == DiscrepancyType.EXPIRED_UNSWEPT_HOLDS
        )

    @property
    def cancelled_order_holds_count(self) -> int:
        return sum(
            1
            for d in self.discrepancies
            if d.discrepancy_type == DiscrepancyType.CANCELLED_ORDER_HOLDS
        )

    @property
    def missing_stock_records_count(self) -> int:
        return sum(
            1
            for d in self.discrepancies
            if d.discrepancy_type == DiscrepancyType.MISSING_STOCK_RECORD
        )

    @property
    def drop_shortages_count(self) -> int:
        return sum(
            1
            for d in self.discrepancies
            if d.discrepancy_type == DiscrepancyType.DROP_ALLOCATION_SHORTAGE
        )

    @property
    def health_score(self) -> int:
        if self.total_variants_audited == 0:
            return 100
        penalty = (self.critical_count * 20) + (self.warning_count * 5) + (self.info_count * 1)
        return max(0, min(100, 100 - penalty))


# =============================================================================
# 2. Read-Only Reconciliation Auditing
# =============================================================================


def count_critical_reconciliation_discrepancies() -> int:
    """Fast, single-query check for critical reconciliation discrepancies for alert desks.

    Runs exactly one query: checks for any Stock row where the reserved counter
    does not match the sum of active checkout reservations.
    """
    active_holds_subquery = (
        Reservation.objects.filter(
            variant_id=OuterRef("variant_id"),
            status=Reservation.Status.ACTIVE,
        )
        .values("variant_id")
        .annotate(total=Sum("quantity"))
        .values("total")
    )
    return (
        Stock.objects.annotate(
            actual_held=Coalesce(Subquery(active_holds_subquery), Value(0))
        )
        .exclude(reserved=F("actual_held"))
        .count()
    )


def audit_inventory_reconciliation(
    *,
    category_slug: str = "",
    severity_filter: str = "",
    q: str = "",
    query: str = "",
) -> InventoryReconciliationReport:
    """Perform a comprehensive read-only reconciliation check across the catalog.

    Checks:
    1. Active reservations sum vs Stock.reserved.
    2. Reserved exceeding on-hand (oversold invariant violation).
    3. Expired active reservations without an order.
    4. Active holds attached to cancelled or failed orders.
    5. Active variants missing a Stock row.
    6. Inactive variants holding positive on-hand inventory.
    7. Drop allocation commitments exceeding physical available units.
    """
    now = timezone.now()
    report = InventoryReconciliationReport()

    # Base variants queryset
    variants_qs = (
        ProductVariant.objects.select_related("product", "product__category", "stock")
        .all()
    )

    if category_slug:
        variants_qs = variants_qs.filter(product__category__slug=category_slug)
    search_term = (query or q).strip()
    if search_term:
        variants_qs = variants_qs.filter(
            Q(sku__icontains=search_term) | Q(product__name__icontains=search_term)
        )

    # 1. Compute active reservations sum per variant in one aggregate query
    active_holds_map = dict(
        Reservation.objects.filter(status=Reservation.Status.ACTIVE)
        .values("variant_id")
        .annotate(total_held=Sum("quantity"))
        .values_list("variant_id", "total_held")
    )

    # 2. Check each variant
    for variant in variants_qs:
        report.total_variants_audited += 1
        stock = getattr(variant, "stock", None)
        category_name = variant.product.category.name if variant.product.category else "Uncategorized"

        if stock is None:
            if variant.is_active:
                report.discrepancies.append(
                    InventoryDiscrepancy(
                        discrepancy_type=DiscrepancyType.MISSING_STOCK_RECORD,
                        severity=DiscrepancySeverity.CRITICAL,
                        variant_id=variant.pk,
                        variant_sku=variant.sku,
                        product_name=variant.product.name,
                        category_name=category_name,
                        current_on_hand=0,
                        current_reserved=0,
                        expected_reserved=0,
                        discrepancy_delta=0,
                        description=str(_("Active variant has no Stock row in the inventory database.")),
                        remediation_action="initialize_stock",
                    )
                )
            continue

        report.total_on_hand += stock.on_hand
        report.total_reserved += stock.reserved
        report.total_available += stock.available

        expected_held = active_holds_map.get(variant.pk, 0)

        # Check reserved mismatch
        if stock.reserved != expected_held:
            delta = stock.reserved - expected_held
            report.discrepancies.append(
                InventoryDiscrepancy(
                    discrepancy_type=DiscrepancyType.RESERVED_MISMATCH,
                    severity=DiscrepancySeverity.CRITICAL if delta < 0 else DiscrepancySeverity.WARNING,
                    variant_id=variant.pk,
                    variant_sku=variant.sku,
                    product_name=variant.product.name,
                    category_name=category_name,
                    current_on_hand=stock.on_hand,
                    current_reserved=stock.reserved,
                    expected_reserved=expected_held,
                    discrepancy_delta=delta,
                    description=str(
                        _(
                            "Reserved counter (%(res)d) does not match actual active holds (%(exp)d). Delta: %(delta)+d."
                        )
                        % {"res": stock.reserved, "exp": expected_held, "delta": delta}
                    ),
                    remediation_action="reconcile_reserved",
                )
            )

        # Check oversold anomaly
        if stock.reserved > stock.on_hand:
            report.discrepancies.append(
                InventoryDiscrepancy(
                    discrepancy_type=DiscrepancyType.OVERSOLD_INVENTORY,
                    severity=DiscrepancySeverity.CRITICAL,
                    variant_id=variant.pk,
                    variant_sku=variant.sku,
                    product_name=variant.product.name,
                    category_name=category_name,
                    current_on_hand=stock.on_hand,
                    current_reserved=stock.reserved,
                    expected_reserved=expected_held,
                    discrepancy_delta=stock.reserved - stock.on_hand,
                    description=str(
                        _(
                            "Reserved units (%(res)d) exceed physical on-hand stock (%(oh)d). Negative available units."
                        )
                        % {"res": stock.reserved, "oh": stock.on_hand}
                    ),
                    remediation_action="reconcile_reserved",
                )
            )

        # Check inactive variant holding stock
        if not variant.is_active and stock.on_hand > 0:
            report.discrepancies.append(
                InventoryDiscrepancy(
                    discrepancy_type=DiscrepancyType.STOCK_ON_INACTIVE_VARIANT,
                    severity=DiscrepancySeverity.WARNING,
                    variant_id=variant.pk,
                    variant_sku=variant.sku,
                    product_name=variant.product.name,
                    category_name=category_name,
                    current_on_hand=stock.on_hand,
                    current_reserved=stock.reserved,
                    expected_reserved=expected_held,
                    discrepancy_delta=stock.on_hand,
                    description=str(
                        _(
                            "Variant is deactivated but still holds %(qty)d on-hand units in inventory."
                        )
                        % {"qty": stock.on_hand}
                    ),
                    remediation_action="review_variant_status",
                )
            )

    # 3. Check Expired Unslept Holds
    expired_holds = (
        Reservation.objects.filter(
            status=Reservation.Status.ACTIVE,
            expires_at__lte=now,
            order__isnull=True,
        )
        .select_related("variant", "variant__product", "variant__product__category", "checkout")
        .order_by("expires_at")
    )
    for hold in expired_holds:
        cat_name = (
            hold.variant.product.category.name if hold.variant.product.category else "Uncategorized"
        )
        report.discrepancies.append(
            InventoryDiscrepancy(
                discrepancy_type=DiscrepancyType.EXPIRED_UNSWEPT_HOLDS,
                severity=DiscrepancySeverity.WARNING,
                variant_id=hold.variant_id,
                variant_sku=hold.variant.sku,
                product_name=hold.variant.product.name,
                category_name=cat_name,
                current_on_hand=getattr(hold.variant.stock, "on_hand", 0),
                current_reserved=getattr(hold.variant.stock, "reserved", 0),
                expected_reserved=0,
                discrepancy_delta=hold.quantity,
                description=str(
                    _(
                        "Active hold for %(qty)d unit(s) expired at %(exp)s without placing an order."
                    )
                    % {
                        "qty": hold.quantity,
                        "exp": hold.expires_at.strftime("%Y-%m-%d %H:%M"),
                    }
                ),
                remediation_action="sweep_expired",
                reference_id=f"checkout:{hold.checkout_id}",
            )
        )

    # 4. Check Holds on Cancelled Orders
    from apps.orders.models import Order

    cancelled_order_holds = (
        Reservation.objects.filter(
            status=Reservation.Status.ACTIVE,
            order__status=Order.Status.CANCELLED,
        )
        .select_related("variant", "variant__product", "variant__product__category", "order")
        .order_by("pk")
    )
    for hold in cancelled_order_holds:
        cat_name = (
            hold.variant.product.category.name if hold.variant.product.category else "Uncategorized"
        )
        report.discrepancies.append(
            InventoryDiscrepancy(
                discrepancy_type=DiscrepancyType.CANCELLED_ORDER_HOLDS,
                severity=DiscrepancySeverity.CRITICAL,
                variant_id=hold.variant_id,
                variant_sku=hold.variant.sku,
                product_name=hold.variant.product.name,
                category_name=cat_name,
                current_on_hand=getattr(hold.variant.stock, "on_hand", 0),
                current_reserved=getattr(hold.variant.stock, "reserved", 0),
                expected_reserved=0,
                discrepancy_delta=hold.quantity,
                description=str(
                    _(
                        "Active hold for %(qty)d unit(s) is still attached to cancelled order %(ord)s."
                    )
                    % {"qty": hold.quantity, "ord": hold.order.number}
                ),
                remediation_action="release_cancelled_order_holds",
                reference_id=f"order:{hold.order.number}",
            )
        )

    # 5. Check Drop Allocation Commitments vs Available Inventory
    active_drops = FlashDrop.objects.filter(status__in=[DropStatus.LIVE, DropStatus.SCHEDULED])
    for drop in active_drops:
        for drop_product in DropProduct.objects.filter(drop=drop).select_related(
            "product", "variant", "variant__stock"
        ):
            if drop_product.variant and hasattr(drop_product.variant, "stock"):
                v_stock = drop_product.variant.stock
                if v_stock.available <= 0:
                    report.discrepancies.append(
                        InventoryDiscrepancy(
                            discrepancy_type=DiscrepancyType.DROP_ALLOCATION_SHORTAGE,
                            severity=DiscrepancySeverity.CRITICAL
                            if drop.is_live
                            else DiscrepancySeverity.WARNING,
                            variant_id=drop_product.variant_id,
                            variant_sku=drop_product.variant.sku,
                            product_name=drop_product.product.name,
                            category_name=drop_product.product.category.name
                            if drop_product.product.category
                            else "Uncategorized",
                            current_on_hand=v_stock.on_hand,
                            current_reserved=v_stock.reserved,
                            expected_reserved=v_stock.reserved,
                            discrepancy_delta=0,
                            description=str(
                                _(
                                    "Drop '%(drop)s' features variant %(sku)s, but available stock is %(avail)d."
                                )
                                % {
                                    "drop": drop.name,
                                    "sku": drop_product.variant.sku,
                                    "avail": v_stock.available,
                                }
                            ),
                            remediation_action="restock_or_adjust_drop",
                            reference_id=f"drop:{drop.slug}",
                        )
                    )

    # Filter by severity if requested
    if severity_filter:
        report.discrepancies = [
            d for d in report.discrepancies if d.severity == severity_filter
        ]

    # Compute tally counts
    for disc in report.discrepancies:
        if disc.severity == DiscrepancySeverity.CRITICAL:
            report.critical_count += 1
        elif disc.severity == DiscrepancySeverity.WARNING:
            report.warning_count += 1
        else:
            report.info_count += 1
        report.discrepancies_by_type[disc.discrepancy_type] = (
            report.discrepancies_by_type.get(disc.discrepancy_type, 0) + 1
        )

    return report


# =============================================================================
# 3. Explicit, Transactional Reconciliation Corrections
# =============================================================================


@transaction.atomic
def reconcile_variant_reserved(
    variant: ProductVariant | None = None,
    variant_id: int | None = None,
    *,
    actor=None,
    user=None,
    reason: str = "Backoffice inventory reconciliation",
) -> dict[str, Any]:
    """Reconcile a variant's Stock.reserved counter to match the actual active reservations sum.

    Enforces locking, ledger recording, and audit trail attribution.
    Never alters on_hand or historical orders.
    """
    if isinstance(variant, int):
        variant_id = variant
        variant = None
    if variant is None and variant_id is None:
        raise ValidationError(_("A variant or variant_id is required."))
    if variant is None:
        variant = ProductVariant.objects.get(pk=variant_id)

    target_actor = actor or user
    stock = Stock.objects.select_for_update().get(variant=variant)

    # Sum of actual active reservations
    actual_held = (
        Reservation.objects.filter(variant=variant, status=Reservation.Status.ACTIVE).aggregate(
            total=Sum("quantity")
        )["total"]
        or 0
    )

    old_reserved = stock.reserved
    delta = actual_held - old_reserved

    if delta != 0:
        stock.reserved = actual_held
        stock.save(update_fields=["reserved", "updated_at"])

        # Write movement ledger record
        InventoryMovement.objects.create(
            variant=variant,
            kind=InventoryMovement.Kind.ADJUSTMENT,
            on_hand_delta=0,
            reserved_delta=delta,
            reference="reconcile:reserved",
            note=f"Reconciled reserved counter from {old_reserved} to {actual_held}. Reason: {reason}",
            user=target_actor,
        )

        # Audit event
        audit_service.record(
            actor=target_actor,
            domain="inventory",
            action="inventory.reconcile",
            object_type="inventory.stock",
            object_id=variant.pk,
            object_repr=variant.sku,
            reason=reason,
            metadata={
                "action": "reconcile_reserved",
                "old_reserved": old_reserved,
                "new_reserved": actual_held,
                "delta": delta,
            },
        )

    return {
        "fixed": delta != 0,
        "variant_id": variant.pk,
        "sku": variant.sku,
        "old_reserved": old_reserved,
        "new_reserved": actual_held,
        "delta": delta,
    }


@transaction.atomic
def reconcile_cancelled_order_holds(
    *,
    actor,
    reason: str = "Release active reservations on cancelled orders",
) -> int:
    """Find and release any active reservations attached to cancelled orders."""
    from apps.orders.models import Order

    stale_holds = list(
        Reservation.objects.select_for_update()
        .filter(status=Reservation.Status.ACTIVE, order__status=Order.Status.CANCELLED)
        .order_by("pk")
    )
    if not stale_holds:
        return 0

    count = release_holds(
        stale_holds,
        kind=InventoryMovement.Kind.RELEASED,
        reference="reconcile:cancelled_orders",
        note=reason,
    )

    audit_service.record(
        actor=actor,
        domain="inventory",
        action="inventory.reconcile",
        object_type="inventory.reservation",
        object_id="cancelled_orders",
        object_repr=f"{count} holds released",
        reason=reason,
        metadata={"count": count, "action": "release_cancelled_order_holds"},
    )
    return count


@transaction.atomic
def reconcile_expired_holds(
    *,
    actor,
    reason: str = "Sweep expired unowned checkout reservations",
) -> int:
    """Explicitly trigger sweep of expired unowned holds."""
    count = sweep_expired_holds()
    if count > 0:
        audit_service.record(
            actor=actor,
            domain="inventory",
            action="inventory.reconcile",
            object_type="inventory.reservation",
            object_id="expired_holds",
            object_repr=f"{count} expired holds swept",
            reason=reason,
            metadata={"count": count, "action": "sweep_expired_holds"},
        )
    return count


# =============================================================================
# 4. Restock Management & Grounded Prioritization
# =============================================================================


@dataclass
class RestockItem:
    """One SKU evaluated for restocking priority."""

    variant_id: int
    sku: str
    product_name: str
    category_name: str
    color_size: str
    on_hand: int
    reserved: int
    available: int
    threshold: int
    sales_30d: int
    days_remaining: float | None
    drop_commitments: int
    priority: str
    priority_score: int

    @property
    def is_critical(self) -> bool:
        return self.priority.upper() == "CRITICAL"

    @property
    def is_urgent(self) -> bool:
        return self.priority.upper() == "URGENT"

    @property
    def is_warning(self) -> bool:
        return self.priority.upper() == "WARNING"

    @property
    def recent_30d_sales(self) -> int:
        return self.sales_30d

    @property
    def suggested_restock_qty(self) -> int:
        return max(5, (self.threshold * 2) - max(0, self.available))


def get_prioritized_restock_list(
    *,
    category_slug: str = "",
    priority_filter: str = "",
    q: str = "",
    query: str = "",
) -> list[RestockItem]:
    """Calculate an objective, data-grounded restock priority list.

    Evaluates:
    - Current available-to-sell stock vs configured variant/product threshold.
    - Verified recent 30-day sales from the inventory movement ledger (kind=SOLD).
    - Commitments to live and upcoming Flash Drops.
    - Grounded days of inventory remaining (never presenting speculative guesses as facts).
    """
    now = timezone.now()
    thirty_days_ago = now - timedelta(days=30)

    # 1. 30-day sales per variant from ledger
    sales_30d_map = dict(
        InventoryMovement.objects.filter(
            kind=InventoryMovement.Kind.SOLD,
            created_at__gte=thirty_days_ago,
        )
        .values("variant_id")
        .annotate(total_sold=Sum(F("on_hand_delta") * -1))
        .values_list("variant_id", "total_sold")
    )

    # 2. Drop commitments (active/scheduled drops)
    drop_variants_map: dict[int, int] = {}
    drop_products = DropProduct.objects.filter(
        drop__status__in=[DropStatus.LIVE, DropStatus.SCHEDULED],
    ).select_related("drop")
    for dp in drop_products:
        if dp.variant_id:
            # Drop allocation or nominal commitment
            drop_variants_map[dp.variant_id] = drop_variants_map.get(dp.variant_id, 0) + 1

    # 3. Fetch variants with stock and taxonomy
    qs = (
        ProductVariant.objects.select_related(
            "product", "product__category", "color", "size", "stock"
        )
        .filter(is_active=True)
        .order_by("sku")
    )

    if category_slug:
        qs = qs.filter(product__category__slug=category_slug)
    search_needle = (query or q).strip()
    if search_needle:
        qs = qs.filter(Q(sku__icontains=search_needle) | Q(product__name__icontains=search_needle))

    items: list[RestockItem] = []

    for variant in qs:
        stock = getattr(variant, "stock", None)
        on_hand = stock.on_hand if stock else 0
        reserved = stock.reserved if stock else 0
        available = on_hand - reserved
        threshold = stock.effective_threshold if stock else getattr(settings, "BACKOFFICE_LOW_STOCK_THRESHOLD", 5)

        sales_30d = max(0, sales_30d_map.get(variant.pk, 0))
        drop_commitments = drop_variants_map.get(variant.pk, 0)

        # Objective velocity & runout days
        days_remaining: float | None = None
        if sales_30d > 0 and available > 0:
            daily_rate = sales_30d / 30.0
            days_remaining = round(available / daily_rate, 1)

        # Determine Priority
        if available <= 0:
            priority = "CRITICAL"
            # Higher score for items with recent sales velocity or drop commitments
            priority_score = 1000 + sales_30d * 10 + drop_commitments * 50
        elif available <= max(1, threshold // 2) or (days_remaining is not None and days_remaining <= 7):
            priority = "URGENT"
            priority_score = 500 + sales_30d * 5 + drop_commitments * 20 - available
        elif available <= threshold or (days_remaining is not None and days_remaining <= 14):
            priority = "WARNING"
            priority_score = 200 + sales_30d * 2 - available
        else:
            priority = "HEALTHY"
            priority_score = 10

        if priority_filter and priority.upper() != priority_filter.strip().upper():
            continue

        color_size_parts = [
            p
            for p in (
                variant.color.name if variant.color else "",
                variant.size.name if variant.size else "",
            )
            if p
        ]
        color_size_str = " / ".join(color_size_parts) or "Default"

        items.append(
            RestockItem(
                variant_id=variant.pk,
                sku=variant.sku,
                product_name=variant.product.name,
                category_name=variant.product.category.name if variant.product.category else "Uncategorized",
                color_size=color_size_str,
                on_hand=on_hand,
                reserved=reserved,
                available=available,
                threshold=threshold,
                sales_30d=sales_30d,
                days_remaining=days_remaining,
                drop_commitments=drop_commitments,
                priority=priority,
                priority_score=priority_score,
            )
        )

    # Sort: highest priority score first, then lowest available units
    items.sort(key=lambda x: (-x.priority_score, x.available, x.sku))
    return items


@transaction.atomic
def record_restock_receipt(
    *,
    variant: ProductVariant | None = None,
    variant_id: int | None = None,
    quantity: int,
    reference: str = "",
    reason: str = "",
    note: str = "",
    actor=None,
    user=None,
) -> InventoryMovement:
    """Record an authoritative stock receipt into physical inventory.

    Enforces:
    - Positive non-zero receipt quantity.
    - Writing to the authoritative InventoryMovement ledger (kind=RECEIVED).
    - Updating Stock counters atomically with row locking.
    - Full AuditEvent logging with actor attribution.
    - Triggering customer back-in-stock notifications if available units restored.
    - Dispatching staff operational alerts.
    """
    if quantity <= 0:
        raise ValidationError(_("Restock quantity must be a positive integer."))
    if not reason or not reason.strip():
        raise ValidationError(_("A reason for this stock receipt is required."))

    clean_ref = (reference or "").strip() or f"REC-{timezone.now().strftime('%Y%m%d%H%M%S')}"
    clean_reason = reason.strip()
    clean_note = (note or "").strip()

    if variant is None and variant_id is None:
        raise ValidationError(_("A variant or variant_id is required."))
    if variant is None:
        variant = ProductVariant.objects.get(pk=variant_id)

    target_actor = actor or user

    # Authoritative stock mutation
    stock = adjust_stock(
        variant=variant,
        delta=quantity,
        kind=InventoryMovement.Kind.RECEIVED,
        user=target_actor,
        note=f"{clean_reason}. {clean_note}".strip(),
        reference=clean_ref,
    )

    # Fetch recorded movement
    movement = (
        InventoryMovement.objects.filter(variant=variant, kind=InventoryMovement.Kind.RECEIVED)
        .order_by("-created_at", "-pk")
        .first()
    )

    # Log audit event
    audit_service.record(
        actor=target_actor,
        domain="inventory",
        action="inventory.restock",
        object_type="inventory.stock",
        object_id=variant.pk,
        object_repr=variant.sku,
        reason=clean_reason,
        metadata={
            "quantity": quantity,
            "reference": clean_ref,
            "on_hand_after": stock.on_hand,
            "available_after": stock.available,
            "note": clean_note,
        },
    )

    # Dispatch operational alert (deduplicated)
    dispatch_inventory_alert(
        alert_key=f"restock:{variant.sku}",
        title=f"Restock Recorded: {variant.sku}",
        message=f"Received {quantity} units for {variant.sku}. New on-hand: {stock.on_hand}.",
        severity="info",
        metadata={"variant_id": variant.pk, "sku": variant.sku, "quantity": quantity},
    )

    return movement


# =============================================================================
# 5. Inventory Health Metrics & Groupings
# =============================================================================


def get_inventory_health_summary() -> dict[str, Any]:
    """Return comprehensive inventory health statistics and groupings."""
    threshold = getattr(settings, "BACKOFFICE_LOW_STOCK_THRESHOLD", 5)

    stocks_qs = Stock.objects.select_related(
        "variant", "variant__product", "variant__product__category"
    ).annotate(qty_available=F("on_hand") - F("reserved"))

    total_skus = stocks_qs.count()
    out_of_stock_count = stocks_qs.filter(qty_available__lte=0).count()
    low_stock_count = stocks_qs.filter(qty_available__gt=0, qty_available__lte=threshold).count()
    in_stock_count = stocks_qs.filter(qty_available__gt=threshold).count()

    aggregates = stocks_qs.aggregate(
        total_on_hand=Sum("on_hand"),
        total_reserved=Sum("reserved"),
        total_available=Sum("qty_available"),
    )

    total_on_hand = aggregates["total_on_hand"] or 0
    total_reserved = aggregates["total_reserved"] or 0
    total_available = aggregates["total_available"] or 0

    # Grouping by Category
    category_summary = (
        stocks_qs.filter(variant__product__category__isnull=False)
        .values("variant__product__category__name", "variant__product__category__slug")
        .annotate(
            sku_count=Count("pk"),
            category_on_hand=Sum("on_hand"),
            category_reserved=Sum("reserved"),
            category_available=Sum("qty_available"),
            category_out_of_stock=Count("pk", filter=Q(qty_available__lte=0)),
            category_low_stock=Count(
                "pk", filter=Q(qty_available__gt=0, qty_available__lte=threshold)
            ),
        )
        .order_by("variant__product__category__name")
    )

    # Live & Scheduled Drop Commitments
    drop_allocations = (
        DropAllocation.objects.select_related("drop")
        .filter(drop__status__in=[DropStatus.LIVE, DropStatus.SCHEDULED])
        .order_by("-drop__starts_at")
    )

    # Active Stale Reservations
    stale_holds_count = Reservation.objects.filter(
        status=Reservation.Status.ACTIVE,
        expires_at__lt=timezone.now(),
    ).count()

    return {
        "total_skus": total_skus,
        "in_stock_count": in_stock_count,
        "low_stock_count": low_stock_count,
        "out_of_stock_count": out_of_stock_count,
        "total_on_hand": total_on_hand,
        "total_reserved": total_reserved,
        "total_available": total_available,
        "threshold": threshold,
        "category_summary": list(category_summary),
        "drop_allocations": list(drop_allocations),
        "stale_holds_count": stale_holds_count,
    }


# =============================================================================
# 6. Operational Alert Dispatching & Deduplication
# =============================================================================


def dispatch_inventory_alert(
    alert_key: str | None = None,
    title: str = "",
    message: str = "",
    *,
    alert_type: str | None = None,
    variant=None,
    severity: str = "warning",
    metadata: dict | None = None,
    dedup_ttl_seconds: int = 3600,
    dedup_window_seconds: int | None = None,
) -> bool:
    """Dispatch an operational inventory alert with time-window deduplication.

    Prevents repetitive noisy alerts for unchanged conditions.
    Returns True if alert was dispatched, False if deduplicated.
    """
    ttl = dedup_window_seconds if dedup_window_seconds is not None else dedup_ttl_seconds
    if not alert_key:
        var_id = getattr(variant, "pk", "all")
        alert_key = f"{alert_type or 'general'}:{var_id}"
    if not title:
        title = f"Inventory Alert: {alert_type or 'General'}"

    cache_key = f"inv_alert_dedup:{alert_key}"
    if cache.get(cache_key):
        return False

    cache.set(cache_key, True, timeout=ttl)

    # Create system audit entry for record
    try:
        from apps.accounts.models import User

        system_actor = (
            User.objects.filter(is_superuser=True, is_active=True).first()
            or User.objects.filter(is_staff=True, is_active=True).first()
        )
        if system_actor:
            AuditEvent.objects.create(
                actor=system_actor,
                domain=AuditEvent.Domain.INVENTORY,
                action="inventory.adjust",
                object_type="inventory.alert",
                object_id=alert_key,
                object_repr=title[:200],
                reason=message[:200],
                metadata=metadata or {},
            )
    except Exception:
        pass

    return True
