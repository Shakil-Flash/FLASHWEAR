"""Catalogue quality audit and intelligence engine (Phase 43).

Automated catalog health scoring, comprehensive multi-attribute audit,
authoritative inventory and visibility consistency checks, and safe
remediation actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import logging
from typing import Sequence

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Prefetch, Q, QuerySet
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models import (
    Category,
    Product,
    ProductImage,
    ProductVariant,
    SizeGuide,
)
from apps.inventory.models import Stock

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


class CatalogIssueSeverity:
    BLOCKING = "blocking"
    WARNING = "warning"
    RECOMMENDATION = "recommendation"

    CHOICES = (
        (BLOCKING, _("Blocking Error")),
        (WARNING, _("Warning")),
        (RECOMMENDATION, _("Recommendation")),
    )


@dataclass
class CatalogIssue:
    """One detected catalog data quality or merchandising issue."""

    code: str
    severity: str
    message: str
    category: str
    product_id: int
    product_name: str
    admin_url: str
    variant_id: int | None = None
    variant_sku: str | None = None

    @property
    def is_blocking(self) -> bool:
        return self.severity == CatalogIssueSeverity.BLOCKING

    @property
    def is_warning(self) -> bool:
        return self.severity == CatalogIssueSeverity.WARNING

    @property
    def is_recommendation(self) -> bool:
        return self.severity == CatalogIssueSeverity.RECOMMENDATION


@dataclass
class ProductQualityAudit:
    """Audit result for a single garment."""

    product: Product
    score: int
    issues: list[CatalogIssue] = field(default_factory=list)

    @property
    def blocking_count(self) -> int:
        return sum(1 for issue in self.issues if issue.is_blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.is_warning)

    @property
    def recommendation_count(self) -> int:
        return sum(1 for issue in self.issues if issue.is_recommendation)

    @property
    def is_publishable(self) -> bool:
        """True if the product has zero blocking commerce-breaking errors."""
        return self.blocking_count == 0


@dataclass
class CatalogAuditReport:
    """Aggregated catalog health report across all or filtered products."""

    total_products: int
    healthy_products_count: int
    blocking_issues_count: int
    warning_issues_count: int
    recommendation_issues_count: int
    issues_by_code: dict[str, int] = field(default_factory=dict)
    issues_by_severity: dict[str, int] = field(default_factory=dict)
    issues_by_category: dict[str, int] = field(default_factory=dict)
    all_issues: list[CatalogIssue] = field(default_factory=list)
    product_audits: dict[int, ProductQualityAudit] = field(default_factory=dict)

    @property
    def health_index(self) -> int:
        """Overall catalog health percentage from 0 to 100."""
        if not self.total_products:
            return 100
        total_score = sum(audit.score for audit in self.product_audits.values())
        return max(0, min(100, round(total_score / self.total_products)))


def get_product_admin_url(product_id: int) -> str:
    """Safely build link to the Django admin change form."""
    try:
        return reverse("admin:catalog_product_change", args=[product_id])
    except Exception:
        return f"/admin/catalog/product/{product_id}/change/"


def audit_product(product: Product) -> ProductQualityAudit:
    """Evaluate data quality and consistency for a single product."""
    issues: list[CatalogIssue] = []
    admin_url = get_product_admin_url(product.pk)
    is_active_product = product.status == Product.Status.ACTIVE

    # Helper for adding issues
    def add_issue(
        code: str,
        severity: str,
        message: str,
        category: str,
        variant_id: int | None = None,
        variant_sku: str | None = None,
    ):
        issues.append(
            CatalogIssue(
                code=code,
                severity=severity,
                message=message,
                category=category,
                product_id=product.pk,
                product_name=product.name,
                admin_url=admin_url,
                variant_id=variant_id,
                variant_sku=variant_sku,
            )
        )

    # 1. Publication & Taxonomy Integrity
    if is_active_product:
        if not product.published_at:
            add_issue(
                "active_missing_published_at",
                CatalogIssueSeverity.BLOCKING,
                _("Product is marked Active but published_at date is missing."),
                "taxonomy",
            )
        elif product.published_at > timezone.now():
            add_issue(
                "scheduled_future_publish",
                CatalogIssueSeverity.WARNING,
                _("Product is scheduled for future launch and not yet live."),
                "taxonomy",
            )

        if not product.category_id:
            add_issue(
                "missing_category",
                CatalogIssueSeverity.BLOCKING,
                _("Product has no category assigned."),
                "taxonomy",
            )
        elif hasattr(product, "category") and product.category and not product.category.is_active:
            add_issue(
                "inactive_category",
                CatalogIssueSeverity.BLOCKING,
                _("Assigned category '%(cat)s' is inactive. Product cannot be live.")
                % {"cat": product.category.name},
                "taxonomy",
            )

        if (
            product.brand_id
            and hasattr(product, "brand")
            and product.brand
            and not product.brand.is_active
        ):
            add_issue(
                "inactive_brand",
                CatalogIssueSeverity.BLOCKING,
                _("Assigned brand '%(brand)s' is inactive.") % {"brand": product.brand.name},
                "taxonomy",
            )
    else:
        # Inactive or Draft
        if not product.category_id:
            add_issue(
                "missing_category",
                CatalogIssueSeverity.WARNING,
                _("Category is not assigned (required before publishing)."),
                "taxonomy",
            )

    # 2. Variants & SKUs Integrity
    variants = getattr(product, "_prefetched_objects_cache", {}).get("variants")
    if variants is not None:
        variants_list = list(variants)
    else:
        variants_list = list(product.variants.select_related("color", "size", "stock").all())

    active_variants = [v for v in variants_list if v.is_active]

    if not variants_list:
        severity = (
            CatalogIssueSeverity.BLOCKING
            if is_active_product
            else CatalogIssueSeverity.WARNING
        )
        add_issue(
            "no_variants",
            severity,
            _("Product has no variants/SKUs created."),
            "variants",
        )
    elif not active_variants:
        severity = (
            CatalogIssueSeverity.BLOCKING
            if is_active_product
            else CatalogIssueSeverity.WARNING
        )
        add_issue(
            "no_active_variants",
            severity,
            _("Product has variants, but none are active for purchase."),
            "variants",
        )

    # Check variant combinations, prices, SKUs, and stock
    seen_combos = set()
    for variant in variants_list:
        # Combination collision check
        combo_key = (variant.color_id, variant.size_id)
        if combo_key in seen_combos:
            add_issue(
                "duplicate_variant_combination",
                CatalogIssueSeverity.BLOCKING,
                _("Duplicate variant combination for options '%(opts)s'.")
                % {"opts": variant.option_label or "Default"},
                "variants",
                variant_id=variant.pk,
                variant_sku=variant.sku,
            )
        seen_combos.add(combo_key)

        # SKU validity
        if not variant.sku or not variant.sku.strip():
            add_issue(
                "empty_sku",
                CatalogIssueSeverity.BLOCKING,
                _("Variant has an empty SKU."),
                "variants",
                variant_id=variant.pk,
                variant_sku=variant.sku,
            )

        # Pricing checks
        if variant.is_active:
            if variant.price is None or variant.price <= ZERO:
                add_issue(
                    "invalid_price",
                    CatalogIssueSeverity.BLOCKING,
                    _("Active variant %(sku)s has zero or non-positive price (%(price)s).")
                    % {"sku": variant.sku, "price": variant.price},
                    "variants",
                    variant_id=variant.pk,
                    variant_sku=variant.sku,
                )
            if (
                variant.compare_at_price is not None
                and variant.compare_at_price <= variant.price
            ):
                add_issue(
                    "invalid_compare_at_price",
                    CatalogIssueSeverity.BLOCKING,
                    _(
                        "Compare-at price (%(was)s) must be greater than selling price (%(price)s) for %(sku)s."
                    )
                    % {
                        "was": variant.compare_at_price,
                        "price": variant.price,
                        "sku": variant.sku,
                    },
                    "variants",
                    variant_id=variant.pk,
                    variant_sku=variant.sku,
                )
            if (
                variant.cost_price is not None
                and variant.price is not None
                and variant.cost_price > variant.price
            ):
                add_issue(
                    "cost_exceeds_price",
                    CatalogIssueSeverity.WARNING,
                    _("Cost price (%(cost)s) exceeds selling price (%(price)s) on %(sku)s.")
                    % {"cost": variant.cost_price, "price": variant.price, "sku": variant.sku},
                    "variants",
                    variant_id=variant.pk,
                    variant_sku=variant.sku,
                )

        # Inventory / Stock checks
        stock = getattr(variant, "stock", None)
        if stock is None:
            # Active variant without a stock counter row
            if variant.is_active:
                add_issue(
                    "missing_stock_record",
                    CatalogIssueSeverity.BLOCKING,
                    _("Variant %(sku)s has no Stock record in inventory system.")
                    % {"sku": variant.sku},
                    "inventory",
                    variant_id=variant.pk,
                    variant_sku=variant.sku,
                )
        else:
            if stock.reserved > stock.on_hand:
                add_issue(
                    "inventory_oversold",
                    CatalogIssueSeverity.BLOCKING,
                    _(
                        "Inventory mismatch on %(sku)s: reserved (%(res)d) exceeds on_hand (%(oh)d)."
                    )
                    % {"sku": variant.sku, "res": stock.reserved, "oh": stock.on_hand},
                    "inventory",
                    variant_id=variant.pk,
                    variant_sku=variant.sku,
                )
            if not variant.is_active and stock.on_hand > 0:
                add_issue(
                    "stock_on_inactive_variant",
                    CatalogIssueSeverity.WARNING,
                    _("Inactive variant %(sku)s still holds %(qty)d units in stock.")
                    % {"sku": variant.sku, "qty": stock.on_hand},
                    "inventory",
                    variant_id=variant.pk,
                    variant_sku=variant.sku,
                )

    # 3. Media & Photography Integrity
    images = getattr(product, "_prefetched_objects_cache", {}).get("images")
    if images is not None:
        images_list = list(images)
    else:
        images_list = list(product.images.all())

    if not images_list:
        severity = (
            CatalogIssueSeverity.BLOCKING
            if is_active_product
            else CatalogIssueSeverity.WARNING
        )
        add_issue(
            "missing_images",
            severity,
            _("Product has no photography or media attached."),
            "images",
        )
    else:
        primary_images = [img for img in images_list if img.is_primary]
        if len(primary_images) == 0:
            add_issue(
                "no_primary_image",
                CatalogIssueSeverity.WARNING,
                _("Product has %(count)d images but none is designated as primary.")
                % {"count": len(images_list)},
                "images",
            )
        elif len(primary_images) > 1:
            add_issue(
                "multiple_primary_images",
                CatalogIssueSeverity.WARNING,
                _("Product has %(count)d primary images designated (should be exactly 1).")
                % {"count": len(primary_images)},
                "images",
            )

        missing_alt = [img for img in images_list if not (img.alt_text or "").strip()]
        if missing_alt:
            add_issue(
                "missing_image_alt",
                CatalogIssueSeverity.WARNING,
                _("%(count)d image(s) missing alt text for screen readers and accessibility.")
                % {"count": len(missing_alt)},
                "images",
            )

        # Check for unassigned or broken image files
        for img in images_list:
            if not img.image and not img.source_url:
                add_issue(
                    "broken_image_record",
                    CatalogIssueSeverity.WARNING,
                    _("Image record #%(id)d has neither an uploaded file nor a source URL.")
                    % {"id": img.pk},
                    "images",
                )

    # 4. Sizing & Size Guide Consistency
    # Retrieve assigned guide: direct product override or category-level guide
    assigned_guide = getattr(product, "size_guide", None)
    if not assigned_guide and hasattr(product, "category") and product.category:
        category_guides = getattr(
            product.category, "_prefetched_objects_cache", {}
        ).get("size_guides")
        if category_guides is not None:
            active_guides = [g for g in category_guides if g.is_active]
            assigned_guide = active_guides[0] if active_guides else None
        else:
            assigned_guide = product.category.size_guides.filter(is_active=True).first()

    if assigned_guide:
        guide_rows = getattr(assigned_guide, "_prefetched_objects_cache", {}).get("rows")
        if guide_rows is not None:
            guide_size_ids = {r.size_id for r in guide_rows}
        else:
            guide_size_ids = set(assigned_guide.rows.values_list("size_id", flat=True))

        for v in active_variants:
            if v.size_id and v.size_id not in guide_size_ids:
                add_issue(
                    "size_guide_mismatch",
                    CatalogIssueSeverity.WARNING,
                    _("Variant size '%(size)s' is not defined in size guide '%(guide)s'.")
                    % {"size": v.size.name if v.size else v.size_id, "guide": assigned_guide.name},
                    "sizing",
                    variant_id=v.pk,
                    variant_sku=v.sku,
                )

    # 5. Editorial & Content Completeness
    if not (product.short_description or "").strip():
        severity = (
            CatalogIssueSeverity.WARNING
            if is_active_product
            else CatalogIssueSeverity.RECOMMENDATION
        )
        add_issue(
            "missing_short_description",
            severity,
            _("Missing short summary description."),
            "content",
        )
    if not (product.description or "").strip():
        severity = (
            CatalogIssueSeverity.WARNING
            if is_active_product
            else CatalogIssueSeverity.RECOMMENDATION
        )
        add_issue(
            "missing_description",
            severity,
            _("Missing full product description and fabric details."),
            "content",
        )

    # 6. SEO & Enrichment Recommendations
    if not (product.seo_title or "").strip():
        add_issue(
            "missing_seo_title",
            CatalogIssueSeverity.RECOMMENDATION,
            _("Missing custom SEO title (falling back to product name)."),
            "seo",
        )
    if not (product.seo_description or "").strip():
        add_issue(
            "missing_seo_description",
            CatalogIssueSeverity.RECOMMENDATION,
            _("Missing custom SEO meta description (falling back to short description)."),
            "seo",
        )
    if not product.fit_id:
        add_issue(
            "missing_fit_attribute",
            CatalogIssueSeverity.RECOMMENDATION,
            _("Fit profile attribute is unassigned."),
            "content",
        )

    # Calculate overall health score (0-100)
    # Deductions: blocking = 25 pts each, warning = 8 pts each, recommendation = 2 pts each
    blocking_penalty = sum(25 for i in issues if i.is_blocking)
    warning_penalty = sum(8 for i in issues if i.is_warning)
    rec_penalty = sum(2 for i in issues if i.is_recommendation)
    score = max(0, 100 - (blocking_penalty + warning_penalty + rec_penalty))

    return ProductQualityAudit(
        product=product,
        score=score,
        issues=issues,
    )


def audit_catalog(
    queryset: QuerySet | None = None,
    *,
    severity: str | None = None,
    issue_code: str | None = None,
    category_slug: str | None = None,
    status: str | None = None,
) -> CatalogAuditReport:
    """Run an optimized catalog quality audit over products with eager prefetching."""
    if queryset is None:
        queryset = Product.objects.all()

    # Pre-filter queryset if filters given
    if status and status in Product.Status.values:
        queryset = queryset.filter(status=status)
    if category_slug:
        queryset = queryset.filter(category__slug=category_slug)

    # Eager loading: loads everything in a fixed set of ~5 queries total
    queryset = (
        queryset.select_related("category", "brand", "size_guide")
        .prefetch_related(
            Prefetch("images", queryset=ProductImage.objects.order_by("position", "id")),
            Prefetch(
                "variants",
                queryset=ProductVariant.objects.select_related(
                    "color", "size", "stock"
                ).order_by("color__display_order", "size__display_order", "sku"),
            ),
            Prefetch(
                "category__size_guides",
                queryset=SizeGuide.objects.filter(is_active=True).prefetch_related("rows"),
            ),
            Prefetch(
                "size_guide__rows",
            ),
        )
        .order_by("-updated_at", "-id")
    )

    all_issues: list[CatalogIssue] = []
    product_audits: dict[int, ProductQualityAudit] = {}
    issues_by_code: dict[str, int] = {}
    issues_by_severity: dict[str, int] = {}
    issues_by_category: dict[str, int] = {}

    healthy_count = 0
    blocking_total = 0
    warning_total = 0
    rec_total = 0

    for prod in queryset:
        audit = audit_product(prod)
        product_audits[prod.pk] = audit

        if audit.is_publishable and audit.score >= 80:
            healthy_count += 1

        for issue in audit.issues:
            # Filter matches
            if severity and issue.severity != severity:
                continue
            if issue_code and issue.code != issue_code:
                continue

            all_issues.append(issue)
            issues_by_code[issue.code] = issues_by_code.get(issue.code, 0) + 1
            issues_by_severity[issue.severity] = (
                issues_by_severity.get(issue.severity, 0) + 1
            )
            issues_by_category[issue.category] = (
                issues_by_category.get(issue.category, 0) + 1
            )

            if issue.is_blocking:
                blocking_total += 1
            elif issue.is_warning:
                warning_total += 1
            elif issue.is_recommendation:
                rec_total += 1

    return CatalogAuditReport(
        total_products=len(product_audits),
        healthy_products_count=healthy_count,
        blocking_issues_count=blocking_total,
        warning_issues_count=warning_total,
        recommendation_issues_count=rec_total,
        issues_by_code=issues_by_code,
        issues_by_severity=issues_by_severity,
        issues_by_category=issues_by_category,
        all_issues=all_issues,
        product_audits=product_audits,
    )


# --------------------------------------------------------------------------------------
# Safe Remediation Actions
# --------------------------------------------------------------------------------------


@transaction.atomic
def ensure_variant_stock(
    variants: Sequence[ProductVariant] | None = None,
    product: Product | None = None,
) -> int:
    """Create missing Stock records with zero on-hand for variants lacking inventory tracking.

    Returns the number of created stock rows. Authoritative and safe: never fabricates stock.
    """
    if variants is None:
        if product:
            variants = list(product.variants.all())
        else:
            variants = list(ProductVariant.objects.filter(stock__isnull=True))

    created_count = 0
    for v in variants:
        stock, created = Stock.objects.get_or_create(
            variant=v,
            defaults={"on_hand": 0, "reserved": 0},
        )
        if created:
            created_count += 1

    return created_count


@transaction.atomic
def normalize_primary_images(products: Sequence[Product] | None = None) -> int:
    """Ensure every product with photography has exactly one primary image.

    Promotes the lowest position image if no primary is flagged, and demotes duplicates.
    Returns the number of products modified.
    """
    if products is None:
        products = list(Product.objects.annotate(n_imgs=Count("images")).filter(n_imgs__gt=0))

    modified_count = 0
    now = timezone.now()

    for p in products:
        images = list(p.images.order_by("position", "id"))
        if not images:
            continue

        primaries = [img for img in images if img.is_primary]
        if len(primaries) == 1:
            continue

        # Demote all, then promote the first by position
        p.images.update(is_primary=False, updated_at=now)
        first_img = images[0]
        first_img.is_primary = True
        first_img.save(update_fields=["is_primary", "updated_at"])
        modified_count += 1

    return modified_count


@transaction.atomic
def populate_fallback_alt_text(products: Sequence[Product] | None = None) -> int:
    """Populate safe, non-invented alt text from product name & variant for blank alt tags.

    E.g. 'FLASHWEAR Oversized Tee - Black / M photograph'.
    Returns the count of updated image records.
    """
    if products is None:
        images_to_update = ProductImage.objects.filter(
            Q(alt_text="") | Q(alt_text__isnull=True)
        ).select_related("product", "variant__color", "variant__size")
    else:
        images_to_update = ProductImage.objects.filter(
            product__in=products
        ).filter(
            Q(alt_text="") | Q(alt_text__isnull=True)
        ).select_related("product", "variant__color", "variant__size")

    updated_count = 0
    for img in images_to_update:
        label_parts = [img.product.name]
        if img.variant:
            if img.variant.option_label:
                label_parts.append(img.variant.option_label)
        fallback = f"{' - '.join(label_parts)} product photo"[:200]
        img.alt_text = fallback
        img.save(update_fields=["alt_text", "updated_at"])
        updated_count += 1

    return updated_count
