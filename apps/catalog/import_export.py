"""Catalog CSV import and export engine for FLASHWEAR admin.

Provides staff-only, transaction-safe, validated bulk import/export for:
- Products
- Variants
- SKU, sizes, colours
- Prices, compare-at prices, cost prices
- Categories, brands, tags
- Authoritative stock on hand
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

from apps.backoffice.models import AuditEvent
from apps.backoffice.services.audit import record as record_audit
from apps.catalog.models import (
    Brand,
    Category,
    Color,
    Product,
    ProductTag,
    ProductVariant,
    Size,
)
from apps.inventory.models import Stock
from apps.inventory.services import adjust_stock

CSV_HEADERS = [
    "product_name",
    "slug",
    "category_slug",
    "brand_name",
    "short_description",
    "description",
    "status",
    "tags",
    "is_featured",
    "is_new",
    "sku",
    "color_name",
    "size_code",
    "price",
    "compare_at_price",
    "cost_price",
    "stock_on_hand",
    "variant_active",
    "seo_title",
    "seo_description",
]


@dataclass
class ImportRowError:
    row_number: int
    sku: str
    message: str


@dataclass
class ImportValidationResult:
    is_valid: bool
    total_rows: int
    valid_rows_count: int
    errors: list[ImportRowError] = field(default_factory=list)
    preview_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)


@dataclass
class ImportExecutionResult:
    success: bool
    products_created: int
    products_updated: int
    variants_created: int
    variants_updated: int
    errors: list[ImportRowError] = field(default_factory=list)


def export_catalog_to_csv(queryset) -> str:
    """Export product queryset and all associated variants to a CSV string."""
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_HEADERS)
    writer.writeheader()

    products = (
        queryset.select_related("category", "brand")
        .prefetch_related(
            "tags",
            "variants__color",
            "variants__size",
            "variants__stock",
        )
        .order_by("name", "id")
    )

    for product in products:
        tags_str = ", ".join(t.name for t in product.tags.all())
        variants = list(product.variants.all())

        base_data = {
            "product_name": product.name,
            "slug": product.slug,
            "category_slug": product.category.slug if product.category else "",
            "brand_name": product.brand.name if product.brand else "",
            "short_description": product.short_description or "",
            "description": product.description or "",
            "status": product.status,
            "tags": tags_str,
            "is_featured": "yes" if product.is_featured else "no",
            "is_new": "yes" if product.is_new else "no",
            "seo_title": product.seo_title or "",
            "seo_description": product.seo_description or "",
        }

        if not variants:
            # Output row for product with no variants
            writer.writerow(
                {
                    **base_data,
                    "sku": "",
                    "color_name": "",
                    "size_code": "",
                    "price": "",
                    "compare_at_price": "",
                    "cost_price": "",
                    "stock_on_hand": "0",
                    "variant_active": "no",
                }
            )
            continue

        for variant in variants:
            stock = getattr(variant, "stock", None)
            on_hand = stock.on_hand if stock else 0
            writer.writerow(
                {
                    **base_data,
                    "sku": variant.sku,
                    "color_name": variant.color.name if variant.color else "",
                    "size_code": variant.size.code if variant.size else "",
                    "price": str(variant.price),
                    "compare_at_price": str(variant.compare_at_price or ""),
                    "cost_price": str(variant.cost_price or ""),
                    "stock_on_hand": str(on_hand),
                    "variant_active": "yes" if variant.is_active else "no",
                }
            )

    return output.getvalue()


def parse_csv_content(csv_text_or_file) -> list[dict[str, str]]:
    """Parse CSV content into list of dictionaries."""
    if hasattr(csv_text_or_file, "read"):
        content = csv_text_or_file.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8-sig", errors="replace")
    else:
        content = str(csv_text_or_file)

    reader = csv.DictReader(StringIO(content))
    return list(reader)


def validate_catalog_csv(csv_text_or_file) -> ImportValidationResult:
    """Validate CSV content row by row without committing any changes."""
    rows = parse_csv_content(csv_text_or_file)
    errors: list[ImportRowError] = []
    seen_skus_in_csv: set[str] = set()

    # Preload categories and brands for fast resolution
    existing_categories = {c.slug: c for c in Category.objects.all()}
    existing_categories.update({c.name.lower(): c for c in Category.objects.all()})
    existing_brands = {b.name.lower(): b for b in Brand.objects.all()}
    existing_brands.update({b.slug: b for b in Brand.objects.all()})
    existing_variants = {
        v.sku: v.product_id for v in ProductVariant.objects.all()
    }
    existing_products = {p.slug: p.id for p in Product.objects.all()}

    preview_rows = []

    for index, row in enumerate(rows, start=2):
        row_clean = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        sku = row_clean.get("sku", "").upper()
        prod_name = row_clean.get("product_name") or row_clean.get("name", "")
        cat_slug = row_clean.get("category_slug") or row_clean.get("category", "")
        price_str = row_clean.get("price", "")

        # 1. Required fields
        if not prod_name:
            errors.append(ImportRowError(index, sku, "Missing required product_name."))
        if not cat_slug:
            errors.append(ImportRowError(index, sku, "Missing required category_slug."))
        elif cat_slug not in existing_categories:
            errors.append(
                ImportRowError(
                    index,
                    sku,
                    f"Category '{cat_slug}' does not exist.",
                )
            )

        if not sku:
            errors.append(ImportRowError(index, sku, "Missing required variant SKU."))
        else:
            # 2. Duplicate detection in CSV
            if sku in seen_skus_in_csv:
                errors.append(
                    ImportRowError(
                        index,
                        sku,
                        f"Duplicate SKU '{sku}' defined multiple times in import file.",
                    )
                )
            seen_skus_in_csv.add(sku)

            # 3. Duplicate detection against other products in DB
            slug = row_clean.get("slug") or slugify(prod_name)
            expected_product_id = existing_products.get(slug)
            actual_product_id = existing_variants.get(sku)
            if (
                actual_product_id
                and expected_product_id
                and actual_product_id != expected_product_id
            ):
                errors.append(
                    ImportRowError(
                        index,
                        sku,
                        f"SKU '{sku}' is already assigned to a different product in database.",
                    )
                )

        # 4. Price validation
        if not price_str:
            errors.append(ImportRowError(index, sku, "Missing required price."))
        else:
            try:
                price = Decimal(price_str)
                if price < 0:
                    errors.append(
                        ImportRowError(index, sku, f"Price '{price_str}' cannot be negative.")
                    )
            except InvalidOperation:
                errors.append(
                    ImportRowError(index, sku, f"Price '{price_str}' is invalid.")
                )

        # Compare at price validation
        compare_str = row_clean.get("compare_at_price", "")
        if compare_str:
            try:
                comp_price = Decimal(compare_str)
                price_val = Decimal(price_str) if price_str else Decimal("0")
                if comp_price <= price_val:
                    errors.append(
                        ImportRowError(
                            index,
                            sku,
                            f"Compare-at price ({comp_price}) must be higher "
                            f"than selling price ({price_val}).",
                        )
                    )
            except InvalidOperation:
                errors.append(
                    ImportRowError(
                        index,
                        sku,
                        f"Invalid decimal for compare_at_price '{compare_str}'.",
                    )
                )

        # Stock validation
        stock_str = row_clean.get("stock_on_hand", "")
        if stock_str:
            try:
                stk = int(stock_str)
                if stk < 0:
                    errors.append(
                        ImportRowError(
                            index,
                            sku,
                            f"Stock on hand '{stock_str}' cannot be negative.",
                        )
                    )
            except ValueError:
                errors.append(
                    ImportRowError(
                        index,
                        sku,
                        f"Stock on hand '{stock_str}' must be an integer.",
                    )
                )

        if len(preview_rows) < 15:
            preview_rows.append(row_clean)

    return ImportValidationResult(
        is_valid=len(errors) == 0,
        total_rows=len(rows),
        valid_rows_count=len(rows) - len(errors),
        errors=errors,
        preview_rows=preview_rows,
    )


def execute_catalog_import(csv_text_or_file, *, user=None) -> ImportExecutionResult:
    """Execute import in a single transaction with validation and audit logging."""
    validation = validate_catalog_csv(csv_text_or_file)
    if not validation.is_valid:
        return ImportExecutionResult(
            success=False,
            products_created=0,
            products_updated=0,
            variants_created=0,
            variants_updated=0,
            errors=validation.errors,
        )

    rows = parse_csv_content(csv_text_or_file)
    products_created = 0
    products_updated = 0
    variants_created = 0
    variants_updated = 0

    with transaction.atomic():
        # Group rows by product slug/name
        for _index, row in enumerate(rows, start=2):
            row_clean = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            prod_name = row_clean.get("product_name") or row_clean.get("name", "")
            slug = row_clean.get("slug") or slugify(prod_name)
            cat_slug = row_clean.get("category_slug") or row_clean.get("category", "")
            try:
                category = Category.objects.get(slug=cat_slug)
            except Category.DoesNotExist:
                category = Category.objects.get(name__iexact=cat_slug)

            brand_name = row_clean.get("brand_name") or row_clean.get("brand", "")
            brand = None
            if brand_name:
                brand = Brand.objects.filter(
                    Q(slug=brand_name) | Q(name__iexact=brand_name)
                ).first()
                if not brand:
                    brand = Brand.objects.create(name=brand_name, slug=slugify(brand_name))

            status_val = row_clean.get("status", Product.Status.DRAFT).lower()
            if status_val not in Product.Status.values:
                status_val = Product.Status.DRAFT

            is_feat = row_clean.get("is_featured", "").lower() in ("yes", "true", "1")
            is_nw = row_clean.get("is_new", "").lower() in ("yes", "true", "1")

            product, created = Product.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": prod_name,
                    "category": category,
                    "brand": brand,
                    "short_description": row_clean.get("short_description", ""),
                    "description": row_clean.get("description", ""),
                    "status": status_val,
                    "is_featured": is_feat,
                    "is_new": is_nw,
                    "seo_title": row_clean.get("seo_title", ""),
                    "seo_description": row_clean.get("seo_description", ""),
                },
            )

            if created:
                products_created += 1
            else:
                # Update product fields
                updated = False
                if product.name != prod_name:
                    product.name = prod_name
                    updated = True
                if product.category_id != category.id:
                    product.category = category
                    updated = True
                if brand and product.brand_id != brand.id:
                    product.brand = brand
                    updated = True
                if updated:
                    product.save()
                    products_updated += 1

            # Handle tags
            tags_str = row_clean.get("tags", "")
            if tags_str:
                tag_names = [t.strip() for t in tags_str.split(",") if t.strip()]
                for t_name in tag_names:
                    t_slug = slugify(t_name)
                    tag_obj, _ = ProductTag.objects.get_or_create(
                        name=t_name, defaults={"slug": t_slug}
                    )
                    product.tags.add(tag_obj)

            # Handle variant
            sku = row_clean["sku"].upper()
            color_name = row_clean.get("color_name") or row_clean.get("color", "")
            color = None
            if color_name:
                color = Color.objects.filter(
                    Q(slug=color_name) | Q(name__iexact=color_name)
                ).first()
                if not color:
                    color = Color.objects.create(name=color_name, slug=slugify(color_name))

            size_code = row_clean.get("size_code") or row_clean.get("size", "")
            size = None
            if size_code:
                size = Size.objects.filter(
                    Q(code__iexact=size_code) | Q(slug=size_code)
                ).first()
                if not size:
                    size = Size.objects.create(
                        code=size_code, name=size_code, slug=slugify(size_code)
                    )

            price = Decimal(row_clean["price"])
            compare_price = (
                Decimal(row_clean["compare_at_price"])
                if row_clean.get("compare_at_price")
                else None
            )
            cost_price = (
                Decimal(row_clean["cost_price"])
                if row_clean.get("cost_price")
                else None
            )
            var_active = row_clean.get("variant_active", "yes").lower() in ("yes", "true", "1")

            variant, var_created = ProductVariant.objects.get_or_create(
                sku=sku,
                defaults={
                    "product": product,
                    "color": color,
                    "size": size,
                    "price": price,
                    "compare_at_price": compare_price,
                    "cost_price": cost_price,
                    "is_active": var_active,
                },
            )

            if var_created:
                variants_created += 1
            else:
                variant.price = price
                variant.compare_at_price = compare_price
                variant.cost_price = cost_price
                variant.is_active = var_active
                if color:
                    variant.color = color
                if size:
                    variant.size = size
                variant.save()
                variants_updated += 1

            # Handle stock on hand
            stock_str = row_clean.get("stock_on_hand", "")
            if stock_str:
                desired_stock = int(stock_str)
                stock_record, _ = Stock.objects.get_or_create(
                    variant=variant,
                    defaults={"on_hand": desired_stock, "reserved": 0},
                )
                if stock_record.on_hand != desired_stock:
                    delta = desired_stock - stock_record.on_hand
                    if delta != 0:
                        adjust_stock(
                            variant,
                            delta,
                            user=user,
                            reference="csv_import",
                            note="Adjusted by CSV import",
                        )

        # Audit logging
        record_audit(
            actor=user,
            domain=AuditEvent.Domain.CATALOG,
            action="catalog.import",
            object_type="catalog.product",
            object_repr=f"Imported {len(rows)} CSV rows",
            metadata={
                "products_created": products_created,
                "products_updated": products_updated,
                "variants_created": variants_created,
                "variants_updated": variants_updated,
            },
        )

    return ImportExecutionResult(
        success=True,
        products_created=products_created,
        products_updated=products_updated,
        variants_created=variants_created,
        variants_updated=variants_updated,
    )
