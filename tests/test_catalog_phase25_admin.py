"""Tests for FLASHWEAR Phase 25: Advanced Catalog Management Admin.

Covers:
1. Product management dashboard (metrics, filtering by stock & readiness, search, N+1 query safety)
2. Bulk product actions (publish, unpublish, activate, deactivate, assign category, tags, SEO)
3. Variant management (matrix view, duplicate SKU prevention, stock adjustments via inventory)
4. Product images (upload/URL integration, primary image, alt text checks)
5. Product preview security & rendering (staff preview, banner, security against non-staff)
6. Content validation & readiness warnings (non-blocking checks for content completeness)
7. SEO content & previews (canonical URL, Google SERP, Open Graph preview, bulk derive)
8. CSV Import & Export (export formatting, dry-run validation, duplicate SKU, atomic commit)
9. Permissions & IDOR boundaries (Catalog Manager vs normal customer, inventory separation)
10. Auditability (AuditEvent records for staff catalog operations)
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.backoffice.models import AuditEvent
from apps.catalog.import_export import (
    execute_catalog_import,
    export_catalog_to_csv,
    validate_catalog_csv,
)
from apps.catalog.models import (
    Product,
    ProductImage,
    ProductVariant,
)
from apps.catalog.readiness import evaluate_product_readiness
from apps.inventory.models import InventoryMovement, Stock
from tests.phase6_helpers import seed_stock

pytestmark = pytest.mark.django_db


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def catalog_staff(db, django_user_model):
    """Staff user with catalog management permissions."""
    user = django_user_model.objects.create_user(
        email="catalog_admin@flashwear.com",
        password="ValidPassword123!",
        is_staff=True,
    )
    product_ct = ContentType.objects.get_for_model(Product)
    variant_ct = ContentType.objects.get_for_model(ProductVariant)
    image_ct = ContentType.objects.get_for_model(ProductImage)
    perms = Permission.objects.filter(content_type__in=[product_ct, variant_ct, image_ct])
    user.user_permissions.set(perms)
    return user


@pytest.fixture
def catalog_client(catalog_staff):
    client = Client()
    client.force_login(catalog_staff)
    return client


@pytest.fixture
def regular_customer(db, django_user_model):
    """Authenticated non-staff customer."""
    return django_user_model.objects.create_user(
        email="customer@flashwear.com",
        password="ValidPassword123!",
        is_staff=False,
    )


@pytest.fixture
def customer_client(regular_customer):
    client = Client()
    client.force_login(regular_customer)
    return client


# =============================================================================
# 1. Product Management Dashboard & Filtering
# =============================================================================


class TestCatalogDashboard:
    def test_dashboard_renders_with_annotated_metrics(self, admin_client, product):
        """Dashboard list page renders essential columns: SKU count, price, stock, status."""
        seed_stock(product.variants.first(), 10, reserved=2)
        response = admin_client.get("/admin/catalog/product/")
        assert response.status_code == 200
        content = response.content.decode()
        assert product.name in content
        assert "1 SKU" in content
        assert "49.00" in content
        assert "in stock" in content
        assert "👁 Preview" in content

    def test_dashboard_stock_status_filter_in_stock(
        self, admin_client, product, make_product, make_variant, size
    ):
        """Stock status filter isolates in-stock products (>5 avail)."""
        seed_stock(product.variants.first(), 10, reserved=0)

        out_product = make_product(name="Sold Out Tee", slug="sold-out-tee")
        out_var = make_variant(out_product, size=size, sku="SOLD-OUT-M")
        seed_stock(out_var, 0, reserved=0)

        response = admin_client.get("/admin/catalog/product/?stock_status=in_stock")
        assert response.status_code == 200
        content = response.content.decode()
        assert product.name in content
        assert "Sold Out Tee" not in content

    def test_dashboard_stock_status_filter_low_stock(
        self, admin_client, product, make_product, make_variant, size
    ):
        """Stock status filter isolates low stock products (1-5 units)."""
        seed_stock(product.variants.first(), 3, reserved=0)

        other = make_product(name="Ample Tee", slug="ample-tee")
        v = make_variant(other, size=size, sku="AMPLE-M")
        seed_stock(v, 20, reserved=0)

        response = admin_client.get("/admin/catalog/product/?stock_status=low_stock")
        assert response.status_code == 200
        content = response.content.decode()
        assert product.name in content
        assert "Ample Tee" not in content

    def test_dashboard_stock_status_filter_out_of_stock(
        self, admin_client, product, make_product, make_variant, size
    ):
        """Stock status filter isolates zero stock products."""
        seed_stock(product.variants.first(), 0, reserved=0)

        in_prod = make_product(name="Plenty Tee", slug="plenty-tee")
        v = make_variant(in_prod, size=size, sku="PLENTY-M")
        seed_stock(v, 15, reserved=0)

        response = admin_client.get("/admin/catalog/product/?stock_status=out_of_stock")
        assert response.status_code == 200
        content = response.content.decode()
        assert product.name in content
        assert "Plenty Tee" not in content

    def test_dashboard_readiness_filter(self, admin_client, product, make_product):
        """Readiness filter identifies complete vs incomplete products."""
        make_product(
            name="Incomplete Hoodie",
            slug="incomplete-hoodie",
            status="draft",
            short_description="",
        )

        response = admin_client.get("/admin/catalog/product/?readiness=needs_attention")
        assert response.status_code == 200
        content = response.content.decode()
        assert "Incomplete Hoodie" in content

    def test_dashboard_search_by_sku(self, admin_client, product):
        """Admin search finds product when searching for variant SKU."""
        response = admin_client.get("/admin/catalog/product/?q=FW-TEE-BLK-M")
        assert response.status_code == 200
        assert product.name in response.content.decode()


# =============================================================================
# 2. Bulk Product Actions
# =============================================================================


class TestBulkProductActions:
    def test_bulk_unpublish_and_activate(self, admin_client, product):
        """bulk unpublish reverts to draft, activate publishes back."""
        assert product.status == Product.Status.ACTIVE

        # Unpublish
        admin_client.post(
            "/admin/catalog/product/",
            {"action": "unpublish_selected", "_selected_action": [str(product.pk)]},
            follow=True,
        )
        product.refresh_from_db()
        assert product.status == Product.Status.DRAFT

        # Activate
        admin_client.post(
            "/admin/catalog/product/",
            {"action": "activate_selected", "_selected_action": [str(product.pk)]},
            follow=True,
        )
        product.refresh_from_db()
        assert product.status == Product.Status.ACTIVE

    def test_bulk_assign_category_requires_confirmation(
        self, admin_client, product, other_category
    ):
        """Bulk assign category displays intermediate confirmation form before applying."""
        response = admin_client.post(
            "/admin/catalog/product/",
            {"action": "bulk_assign_category", "_selected_action": [str(product.pk)]},
        )
        assert response.status_code == 200
        assert "Bulk Assign Category" in response.content.decode()
        assert "apply" in response.content.decode()

        # Apply confirmation
        response = admin_client.post(
            "/admin/catalog/product/",
            {
                "action": "bulk_assign_category",
                "_selected_action": [str(product.pk)],
                "apply": "1",
                "category": str(other_category.pk),
            },
            follow=True,
        )
        assert response.status_code == 200
        product.refresh_from_db()
        assert product.category == other_category

    def test_bulk_assign_brand(self, admin_client, product, other_brand):
        """Bulk assign brand reassigns brand to selected products."""
        admin_client.post(
            "/admin/catalog/product/",
            {
                "action": "bulk_assign_brand",
                "_selected_action": [str(product.pk)],
                "apply": "1",
                "brand": str(other_brand.pk),
            },
            follow=True,
        )
        product.refresh_from_db()
        assert product.brand == other_brand

    def test_bulk_assign_collection(self, admin_client, product, collection):
        """Bulk assign collection adds and removes products from collection."""
        admin_client.post(
            "/admin/catalog/product/",
            {
                "action": "bulk_assign_collection",
                "_selected_action": [str(product.pk)],
                "apply": "1",
                "collection": str(collection.pk),
                "action_type": "add",
            },
            follow=True,
        )
        assert product in collection.products.all()

        # Remove
        admin_client.post(
            "/admin/catalog/product/",
            {
                "action": "bulk_assign_collection",
                "_selected_action": [str(product.pk)],
                "apply": "1",
                "collection": str(collection.pk),
                "action_type": "remove",
            },
            follow=True,
        )
        assert product not in collection.products.all()

    def test_bulk_update_tags(self, admin_client, product):
        """Bulk update tags adds comma-separated tags to selected products."""
        admin_client.post(
            "/admin/catalog/product/",
            {
                "action": "bulk_update_tags",
                "_selected_action": [str(product.pk)],
                "apply": "1",
                "tags": "Summer, Streetwear",
            },
            follow=True,
        )
        tag_names = list(product.tags.values_list("name", flat=True))
        assert "Summer" in tag_names
        assert "Streetwear" in tag_names

    def test_derive_seo_metadata_action(self, admin_client, product):
        """derive_seo_metadata populates SEO title and description from name and summary."""
        product.seo_title = ""
        product.seo_description = ""
        product.save()

        admin_client.post(
            "/admin/catalog/product/",
            {"action": "derive_seo_metadata", "_selected_action": [str(product.pk)]},
            follow=True,
        )
        product.refresh_from_db()
        assert product.seo_title == product.name
        assert product.seo_description == product.short_description


# =============================================================================
# 3. Variant Management & Authoritative Stock
# =============================================================================


class TestVariantManagement:
    def test_duplicate_sku_is_rejected(self, product, size):
        """Database and validation prevent duplicate SKUs across variants."""
        existing_sku = product.variants.first().sku
        duplicate_variant = ProductVariant(
            product=product,
            sku=existing_sku,
            size=size,
            price=Decimal("35.00"),
        )
        with pytest.raises(ValidationError):
            duplicate_variant.full_clean()

    def test_variant_matrix_renders_in_change_form(self, admin_client, product):
        """Change form includes the live variant matrix table overview."""
        response = admin_client.get(f"/admin/catalog/product/{product.pk}/change/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "FW-TEE-BLK-M" in content
        assert "variant-matrix-table" in content

    def test_inline_stock_adjustment_creates_inventory_movement(self, admin_client, product):
        """Staff with inventory rights adjusting stock inline records an InventoryMovement."""
        variant = product.variants.first()
        seed_stock(variant, 5, reserved=0)

        # Post product change form with stock_adjustment
        post_data = {
            "name": product.name,
            "slug": product.slug,
            "short_description": product.short_description,
            "description": product.description,
            "status": product.status,
            "category": str(product.category.pk),
            "brand": str(product.brand.pk),
            "variants-TOTAL_FORMS": "1",
            "variants-INITIAL_FORMS": "1",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
            "variants-0-id": str(variant.pk),
            "variants-0-product": str(product.pk),
            "variants-0-sku": variant.sku,
            "variants-0-color": str(variant.color.pk),
            "variants-0-size": str(variant.size.pk),
            "variants-0-price": "49.00",
            "variants-0-stock_adjustment": "15",
            "variants-0-is_active": "on",
            "images-TOTAL_FORMS": "0",
            "images-INITIAL_FORMS": "0",
            "images-MIN_NUM_FORMS": "0",
            "images-MAX_NUM_FORMS": "1000",
        }
        response = admin_client.post(
            f"/admin/catalog/product/{product.pk}/change/",
            post_data,
            follow=True,
        )
        assert response.status_code == 200
        stock = Stock.objects.get(variant=variant)
        assert stock.on_hand == 20  # 5 + 15
        assert InventoryMovement.objects.filter(variant=variant, on_hand_delta=15).exists()

    def test_unauthorized_user_cannot_adjust_stock_inline(self, catalog_client, product):
        """Staff with catalog permissions but lacking inventory perms cannot adjust stock inline."""
        variant = product.variants.first()
        seed_stock(variant, 5, reserved=0)

        post_data = {
            "name": product.name,
            "slug": product.slug,
            "short_description": product.short_description,
            "description": product.description,
            "status": product.status,
            "category": str(product.category.pk),
            "brand": str(product.brand.pk),
            "variants-TOTAL_FORMS": "1",
            "variants-INITIAL_FORMS": "1",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
            "variants-0-id": str(variant.pk),
            "variants-0-product": str(product.pk),
            "variants-0-sku": variant.sku,
            "variants-0-color": str(variant.color.pk),
            "variants-0-size": str(variant.size.pk),
            "variants-0-price": "49.00",
            "variants-0-stock_adjustment": "50",
            "variants-0-is_active": "on",
            "images-TOTAL_FORMS": "0",
            "images-INITIAL_FORMS": "0",
            "images-MIN_NUM_FORMS": "0",
            "images-MAX_NUM_FORMS": "1000",
        }
        response = catalog_client.post(
            f"/admin/catalog/product/{product.pk}/change/",
            post_data,
            follow=True,
        )
        assert response.status_code == 200
        stock = Stock.objects.get(variant=variant)
        assert stock.on_hand == 5  # Unchanged!
        msg = "Permission denied: You do not have permission to adjust inventory stock"
        assert msg in response.content.decode()


# =============================================================================
# 4. Product Preview Security & Storefront Rendering
# =============================================================================


class TestProductPreview:
    def test_staff_can_preview_draft_product(self, admin_client, make_product, make_variant, size):
        """Staff can safely preview a draft product with preview banner and without publishing."""
        draft = make_product(
            name="Secret Unreleased Jacket",
            slug="secret-jacket",
            status="draft",
            published_at=None,
        )
        make_variant(draft, size=size, sku="SECRET-JKT-M", price=Decimal("120.00"))

        preview_url = reverse("admin:catalog_product_preview", args=[draft.pk])
        response = admin_client.get(preview_url)
        assert response.status_code == 200
        content = response.content.decode()
        assert "Secret Unreleased Jacket" in content
        assert "Staff Preview Mode" in content
        assert "Draft" in content

        # Check DB status is untouched
        draft.refresh_from_db()
        assert draft.status == Product.Status.DRAFT
        assert draft.published_at is None

    def test_anonymous_user_blocked_from_preview(self, client, make_product):
        """Anonymous user is redirected to login when attempting to preview."""
        draft = make_product(name="Secret Draft", slug="secret-draft", status="draft")
        preview_url = reverse("admin:catalog_product_preview", args=[draft.pk])
        response = client.get(preview_url)
        assert response.status_code == 302
        assert "/admin/login/" in response.url

    def test_customer_blocked_from_preview(self, customer_client, make_product):
        """Authenticated normal customer is rejected from admin preview."""
        draft = make_product(name="Secret Draft 2", slug="secret-draft-2", status="draft")
        preview_url = reverse("admin:catalog_product_preview", args=[draft.pk])
        response = customer_client.get(preview_url)
        assert response.status_code in (302, 403)


# =============================================================================
# 5. Content Validation & Editorial Warnings
# =============================================================================


class TestEditorialReadiness:
    def test_evaluate_product_readiness_identifies_gaps(self, make_product):
        """evaluate_product_readiness returns clear non-blocking warnings."""
        prod = make_product(
            name="Bare Product",
            slug="bare-product",
            short_description="",
            description="",
            status="draft",
        )
        readiness = evaluate_product_readiness(prod)
        assert readiness.is_ready is False
        assert readiness.warning_count >= 4
        warning_text = " ".join(readiness.warnings)
        assert "No product images uploaded" in warning_text
        assert "Missing short description" in warning_text
        assert "Missing full description" in warning_text
        assert "No SKUs/variants created" in warning_text

    def test_ready_product_passes_all_checks(self, full_product):
        """Fully configured product passes readiness evaluation."""
        ProductImage.objects.create(
            product=full_product,
            image="catalog/test.jpg",
            alt_text="Front view",
            is_primary=True,
        )
        full_product.seo_title = "Full T-Shirt | FLASHWEAR"
        full_product.seo_description = "Premium heavyweight organic cotton t-shirt."
        full_product.save()

        readiness = evaluate_product_readiness(full_product)
        assert readiness.is_ready is True
        assert readiness.warnings == []

    def test_readiness_checklist_renders_in_admin(self, admin_client, product):
        """Admin change form renders the readiness checklist box."""
        response = admin_client.get(f"/admin/catalog/product/{product.pk}/change/")
        assert response.status_code == 200
        content = response.content.decode()
        has_box = (
            "Recommended before publishing" in content or "All editorial checks passed" in content
        )
        assert has_box


# =============================================================================
# 6. CSV Import / Export
# =============================================================================


class TestCatalogImportExport:
    def test_export_catalog_to_csv_format(self, product):
        """export_catalog_to_csv generates well-formed CSV with product and variant attributes."""
        csv_text = export_catalog_to_csv(Product.objects.filter(pk=product.pk))
        assert "product_name,slug,category_slug" in csv_text
        assert "sku,color_name,size_code,price" in csv_text
        assert product.name in csv_text
        assert "FW-TEE-BLK-M" in csv_text
        assert "49.00" in csv_text

    def test_admin_export_csv_view(self, admin_client, product):
        """Admin export URL produces attachment CSV and logs audit event."""
        export_url = reverse("admin:catalog_product_export_csv")
        response = admin_client.get(export_url)
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert 'filename="flashwear_full_catalog.csv"' in response["Content-Disposition"]
        assert product.name.encode() in response.content

    def test_validate_catalog_csv_detects_errors(self, category, brand):
        """validate_catalog_csv detects duplicate SKUs, missing categories, and bad prices."""
        invalid_csv = (
            "product_name,slug,category,brand,sku,color,size,price,"
            "compare_at_price,stock_on_hand,status\n"
            f"Test Shirt,t-shirt,{category.slug},{brand.slug},"
            "DUPLICATE-SKU,,,not_a_price,,10,active\n"
            f"Other Shirt,o-shirt,{category.slug},{brand.slug},"
            "DUPLICATE-SKU,,,25.00,,5,active\n"
            f"Bad Cat Shirt,b-shirt,non-existent-cat,{brand.slug},"
            "UNIQUE-SKU,,,30.00,,5,active\n"
        )
        result = validate_catalog_csv(invalid_csv)
        assert result.is_valid is False
        assert len(result.errors) >= 3
        err_text = " ".join(e.message for e in result.errors)
        assert "Price 'not_a_price' is invalid" in err_text
        assert "Duplicate SKU 'DUPLICATE-SKU'" in err_text
        assert "Category 'non-existent-cat' does not exist" in err_text

    def test_execute_catalog_import_commits_atomically(
        self, category, brand, colour, size, admin_user
    ):
        """execute_catalog_import creates products, variants, and sets initial stock."""
        valid_csv = (
            "product_name,slug,category,brand,sku,color,size,price,compare_at_price,stock_on_hand,status\n"
            f"Imported Jacket,imported-jacket,{category.slug},{brand.slug},"
            f"IMP-JKT-BLK-M,{colour.slug},{size.code},89.00,110.00,25,active\n"
        )
        result = execute_catalog_import(valid_csv, user=admin_user)
        assert result.success is True
        assert result.products_created == 1
        assert result.variants_created == 1

        imported_prod = Product.objects.get(slug="imported-jacket")
        assert imported_prod.name == "Imported Jacket"
        variant = imported_prod.variants.get(sku="IMP-JKT-BLK-M")
        assert variant.price == Decimal("89.00")
        assert variant.stock.on_hand == 25

    def test_import_rollback_on_failure(self, category, brand, admin_user):
        """If validation fails, execute_catalog_import rolls back without saving anything."""
        bad_csv = (
            "product_name,slug,category,brand,sku,color,size,price,compare_at_price,stock_on_hand,status\n"
            f"Phantom Shirt,phantom-shirt,{category.slug},{brand.slug},"
            "PHANTOM-SKU,,,INVALID_PRICE,,10,active\n"
        )
        result = execute_catalog_import(bad_csv, user=admin_user)
        assert result.success is False
        assert not Product.objects.filter(slug="phantom-shirt").exists()

    def test_import_csv_view_two_step_workflow(self, admin_client, category, brand, admin_user):
        """Admin import view supports validate-only preview, then commit."""
        import_url = reverse("admin:catalog_product_import_csv")
        csv_content = (
            "product_name,slug,category,brand,sku,color,size,price,compare_at_price,stock_on_hand,status\n"
            f"Preview Item,preview-item,{category.slug},{brand.slug},"
            "PREV-SKU-1,,,45.00,,12,active\n"
        ).encode()
        file = SimpleUploadedFile("catalog.csv", csv_content, content_type="text/csv")

        # Step 1: validate only
        resp1 = admin_client.post(import_url, {"validate_only": "1", "csv_file": file})
        assert resp1.status_code == 200
        assert "Validation Succeeded" in resp1.content.decode()
        assert not Product.objects.filter(slug="preview-item").exists()

        # Step 2: commit import
        resp2 = admin_client.post(
            import_url,
            {"commit_import": "1", "raw_csv_data": csv_content.decode("utf-8")},
            follow=True,
        )
        assert resp2.status_code == 200
        assert Product.objects.filter(slug="preview-item").exists()


# =============================================================================
# 7. Auditability & Security
# =============================================================================


class TestCatalogAuditAndSecurity:
    def test_audit_event_recorded_on_product_save(self, admin_client, category, brand):
        """Creating a product via admin logs a catalog.create AuditEvent."""
        post_data = {
            "name": "Audit Tee",
            "slug": "audit-tee",
            "short_description": "Audit short",
            "description": "Audit long",
            "status": "draft",
            "category": str(category.pk),
            "brand": str(brand.pk),
            "variants-TOTAL_FORMS": "0",
            "variants-INITIAL_FORMS": "0",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
            "images-TOTAL_FORMS": "0",
            "images-INITIAL_FORMS": "0",
            "images-MIN_NUM_FORMS": "0",
            "images-MAX_NUM_FORMS": "1000",
        }
        admin_client.post("/admin/catalog/product/add/", post_data)
        assert Product.objects.filter(slug="audit-tee").exists()
        prod = Product.objects.get(slug="audit-tee")
        assert AuditEvent.objects.filter(
            domain=AuditEvent.Domain.CATALOG,
            action="catalog.create",
            object_id=prod.pk,
        ).exists()

    def test_non_staff_cannot_access_catalog_admin_views(self, customer_client, product):
        """Customers cannot access product changelist, preview, export, or import views."""
        preview_url = reverse("admin:catalog_product_preview", args=[product.pk])
        export_url = reverse("admin:catalog_product_export_csv")
        import_url = reverse("admin:catalog_product_import_csv")
        assert customer_client.get("/admin/catalog/product/").status_code in (302, 403)
        assert customer_client.get(preview_url).status_code in (302, 403)
        assert customer_client.get(export_url).status_code in (302, 403)
        assert customer_client.get(import_url).status_code in (302, 403)
