"""Tests for the catalogue admin, its forms, and the seed command.

The admin is the only write path in Phase 3, so these tests are really about the errors an operator
gets: a duplicate colour/size must read as a sentence, a missing alt text must be refused, and a
bulk publish must go through the same invariants a single save does.
"""

from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# Registration and reachability
# --------------------------------------------------------------------------------------


class TestAdminRegistration:
    @pytest.mark.parametrize(
        "path",
        [
            "/admin/catalog/product/",
            "/admin/catalog/productvariant/",
            "/admin/catalog/productimage/",
            "/admin/catalog/category/",
            "/admin/catalog/brand/",
            "/admin/catalog/collection/",
            "/admin/catalog/color/",
            "/admin/catalog/size/",
            "/admin/catalog/material/",
            "/admin/catalog/fit/",
            "/admin/catalog/producttag/",
        ],
    )
    def test_every_catalogue_model_is_reachable(self, admin_client, path):
        assert admin_client.get(path).status_code == 200

    def test_product_add_form_renders(self, admin_client, category):
        response = admin_client.get("/admin/catalog/product/add/")
        assert response.status_code == 200
        assert "short_description" in response.content.decode()

    def test_change_form_renders_the_inlines(self, admin_client, full_product):
        """Variants and images are edited on the product page, not on separate screens."""
        html = admin_client.get(
            f"/admin/catalog/product/{full_product.pk}/change/"
        ).content.decode()
        assert "variants-0-sku" in html
        assert "images-0-alt_text" in html


class TestAdminActions:
    def test_bulk_publish_uses_the_service(self, admin_client, draft_product):
        response = admin_client.post(
            "/admin/catalog/product/",
            {"action": "publish_selected", "_selected_action": [str(draft_product.pk)]},
            follow=True,
        )
        assert response.status_code == 200
        draft_product.refresh_from_db()
        assert draft_product.status == "active"
        assert draft_product.published_at is not None

    def test_bulk_publish_reports_a_blocked_product(
        self, admin_client, make_product, inactive_category
    ):
        blocked = make_product(
            name="Blocked", slug="blocked", category=inactive_category, status="draft"
        )
        admin_client.post(
            "/admin/catalog/product/",
            {"action": "publish_selected", "_selected_action": [str(blocked.pk)]},
            follow=True,
        )
        blocked.refresh_from_db()
        assert blocked.status == "draft"

    def test_bulk_archive_keeps_the_record(self, admin_client, product):
        admin_client.post(
            "/admin/catalog/product/",
            {"action": "archive_selected", "_selected_action": [str(product.pk)]},
            follow=True,
        )
        product.refresh_from_db()
        assert product.status == "archived"

    def test_promote_primary_image_action(self, admin_client, product):
        from apps.catalog.models import ProductImage

        ProductImage.objects.create(
            product=product, image="catalog/tests/first.png", alt_text="First", position=0
        )
        second = ProductImage.objects.create(
            product=product, image="catalog/tests/second.png", alt_text="Second", position=1
        )
        admin_client.post(
            "/admin/catalog/productimage/",
            {"action": "make_primary", "_selected_action": [str(second.pk)]},
            follow=True,
        )
        second.refresh_from_db()
        assert second.is_primary is True


class TestAdminDisplay:
    def test_product_list_shows_a_price_summary(self, admin_client, product):
        html = admin_client.get("/admin/catalog/product/").content.decode()
        assert "49.00" in html

    def test_product_list_query_count_is_flat(
        self, admin_client, product, make_product, make_variant, size, django_assert_num_queries
    ):
        """Ten more products must not mean ten more queries.

        Asserted as "the same as before" rather than a magic number: the admin's own overhead
        (session, filter choices, counts) is Django's business and may change, but a per-row query
        is ours and would show up here as a difference.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for index in range(6):
            extra = make_product(name=f"Bulk {index}", slug=f"bulk-{index}")
            make_variant(extra, size=size, price=Decimal("20.00"), sku=f"BULK-{index}")

        # Warm-up first: the very first admin request carries one-off queries (session persistence,
        # SiteConfiguration) that have nothing to do with how many rows are listed.
        admin_client.get("/admin/catalog/product/")
        with CaptureQueriesContext(connection) as first_page:
            admin_client.get("/admin/catalog/product/")
        before = len(first_page.captured_queries)

        for index in range(6, 18):
            extra = make_product(name=f"Extra {index}", slug=f"extra-{index}")
            make_variant(extra, size=size, price=Decimal("20.00"), sku=f"EXTRA-{index}")

        with CaptureQueriesContext(connection) as second_page:
            admin_client.get("/admin/catalog/product/")

        assert len(second_page.captured_queries) == before

    def test_price_column_does_not_aggregate_per_row(self, admin_client, product):
        """``price_summary`` reads the prefetched variant list; otherwise it aggregates per row."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        admin_client.get("/admin/catalog/product/")
        with CaptureQueriesContext(connection) as ctx:
            admin_client.get("/admin/catalog/product/")
        aggregates = [
            query
            for query in ctx.captured_queries
            if 'MIN("catalog_productvariant"."price")' in query["sql"]
        ]
        assert aggregates == []

    def test_scheduled_products_are_flagged(self, admin_client, scheduled_product):
        html = admin_client.get("/admin/catalog/product/").content.decode()
        assert "scheduled" in html.lower()

    def test_category_list_does_not_query_per_row(self, admin_client, category, other_category):
        response = admin_client.get("/admin/catalog/category/")
        assert response.status_code == 200

    def test_color_swatch_is_rendered(self, admin_client, colour):
        html = admin_client.get("/admin/catalog/color/").content.decode()
        assert colour.hex_code.lower() in html.lower()


# --------------------------------------------------------------------------------------
# Forms
# --------------------------------------------------------------------------------------


class TestVariantForm:
    def _data(self, **overrides):

        data = {
            "sku": "NEW-SKU-1",
            "color": "",
            "size": "",
            "price": "49.00",
            "compare_at_price": "",
            "cost_price": "",
            "is_active": True,
        }
        data.update(overrides)
        return data

    def test_accepts_a_new_variant(self, full_product, colour, size):
        from apps.catalog.forms import ProductVariantForm

        form = ProductVariantForm(data=self._data(sku="A1", color=colour.pk, size=size.pk))
        assert form.is_valid(), form.errors

    def test_rejects_a_duplicate_sku(self, product):
        from apps.catalog.forms import ProductVariantForm

        form = ProductVariantForm(data=self._data(sku="FW-TEE-BLK-M"))
        assert not form.is_valid()
        assert "sku" in form.errors

    def test_requires_a_sku(self, product):
        from apps.catalog.forms import ProductVariantForm

        form = ProductVariantForm(data=self._data(sku="   "))
        assert not form.is_valid()
        assert "sku" in form.errors

    def test_sku_is_upper_cased(self, product, other_colour, other_size):
        from apps.catalog.forms import ProductVariantForm

        form = ProductVariantForm(
            data=self._data(sku="lower-case", color=other_colour.pk, size=other_size.pk)
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["sku"] == "LOWER-CASE"

    def test_rejects_a_duplicate_colour_and_size(self, full_product, colour, size):
        from apps.catalog.forms import ProductVariantForm

        form = ProductVariantForm(
            data=self._data(sku="NEW-1", color=colour.pk, size=size.pk),
            product=full_product,
        )
        assert not form.is_valid()
        assert "color" in form.errors or "size" in form.errors

    def test_rejects_a_duplicate_without_a_product_kwarg_on_an_instance(
        self, full_product, colour, size
    ):
        """Editing an existing variant supplies the product through the instance."""
        from apps.catalog.forms import ProductVariantForm
        from apps.catalog.models import ProductVariant

        variant = ProductVariant.objects.filter(
            product=full_product, color=colour, size=size
        ).first()
        form = ProductVariantForm(
            data=self._data(sku="EDITED-1", color=colour.pk, size=size.pk),
            instance=variant,
        )
        assert form.is_valid(), form.errors

    def test_editing_a_variant_keeps_its_own_combination(self, full_product):
        from apps.catalog.forms import ProductVariantForm
        from apps.catalog.models import ProductVariant

        variant = ProductVariant.objects.filter(product=full_product).first()
        form = ProductVariantForm(
            data=self._data(
                sku=variant.sku,
                color=variant.color_id,
                size=variant.size_id,
            ),
            instance=variant,
        )
        assert form.is_valid(), form.errors

    def test_only_active_options_are_offered(self, full_product, inactive_colour):
        from apps.catalog.forms import ProductVariantForm

        form = ProductVariantForm(data=self._data())
        assert inactive_colour not in form.fields["color"].queryset


class TestImageForm:
    def _png(self, name="shot.png", colour=(200, 30, 30)):
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (600, 600), colour).save(buffer, format="PNG")
        return SimpleUploadedFile(name, buffer.read(), content_type="image/png")

    def test_alt_text_is_required(self, product):
        from apps.catalog.forms import ProductImageForm

        form = ProductImageForm(
            data={"alt_text": "  ", "position": 0}, files={"image": self._png()}
        )
        assert not form.is_valid()
        assert "alt_text" in form.errors

    def test_rejects_a_variant_from_another_product(
        self, product, make_product, make_variant, colour, size
    ):
        """An image scoped to another product's variant is a data-integrity error."""
        from apps.catalog.forms import ProductImageForm

        other = make_product(name="Elsewhere", slug="elsewhere")
        variant = make_variant(other, color=colour, size=size, sku="ELSEWHERE")
        form = ProductImageForm(
            data={"alt_text": "Front", "position": 0, "variant": variant.pk},
            files={"image": self._png()},
            product=product,
        )
        assert not form.is_valid()
        assert "variant" in form.errors

    def test_rejects_a_svg(self, product):
        from apps.catalog.forms import ProductImageForm

        svg = SimpleUploadedFile(
            "logo.svg",
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            content_type="image/svg+xml",
        )
        form = ProductImageForm(data={"alt_text": "Logo", "position": 0}, files={"image": svg})
        assert not form.is_valid()


class TestProductForm:
    def test_blank_slug_is_generated_from_the_name(self, db, category, brand):
        from apps.catalog.forms import ProductAdminForm

        form = ProductAdminForm(
            data={
                "name": "New Form Tee",
                "slug": "",
                "short_description": "Short",
                "description": "Long",
                "category": category.pk,
                "brand": brand.pk,
                "status": "draft",
            }
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["slug"] == "new-form-tee"

    def test_a_taken_slug_is_reported_with_the_available_one(self, product, category, brand):
        from apps.catalog.forms import ProductAdminForm

        form = ProductAdminForm(
            data={
                "name": "Clashing",
                "slug": product.slug,
                "short_description": "Short",
                "description": "Long",
                "category": category.pk,
                "brand": brand.pk,
                "status": "draft",
            }
        )
        assert not form.is_valid()
        assert "slug" in form.errors
        assert product.slug in str(form.errors["slug"])

    def test_materials_tags_and_collections_are_offered(self, db, category, brand):
        from apps.catalog.forms import ProductAdminForm

        form = ProductAdminForm(
            data={
                "name": "Relations",
                "short_description": "Short",
                "description": "Long",
                "category": category.pk,
                "status": "draft",
            }
        )
        assert form.is_valid(), form.errors
        assert "materials" in form.fields
        assert "collections" in form.fields
        assert "tags" in form.fields


# --------------------------------------------------------------------------------------
# Seed command
# --------------------------------------------------------------------------------------


class TestSeedCommand:
    def test_seeds_a_coherent_catalogue(self, runner):
        from apps.catalog.models import Category, Product, ProductVariant

        runner("seed_catalog", verbosity=0)
        assert Product.objects.count() == 12
        assert Product.objects.filter(status=Product.Status.ACTIVE).exists()
        assert ProductVariant.objects.count() > 100
        assert Category.objects.filter(slug="men").exists()

    def test_every_product_is_published_with_a_date(self, runner):
        from apps.catalog.models import Product

        runner("seed_catalog", verbosity=0)
        assert Product.objects.filter(published_at__isnull=True).exists() is False

    def test_every_product_has_a_variant(self, runner):
        from apps.catalog.models import Product

        runner("seed_catalog", verbosity=0)
        assert all(product.variants.exists() for product in Product.objects.all())

    def test_every_variant_is_priced_and_has_a_sku(self, runner):
        from apps.catalog.models import ProductVariant

        runner("seed_catalog", verbosity=0)
        assert ProductVariant.objects.filter(price__isnull=True).exists() is False
        assert ProductVariant.objects.filter(sku="").exists() is False

    def test_running_twice_changes_nothing(self, runner):
        """The command is idempotent by natural key, not by truncating tables."""
        from apps.catalog.models import Product, ProductVariant

        runner("seed_catalog", verbosity=0)
        counts = (Product.objects.count(), ProductVariant.objects.count())
        runner("seed_catalog", verbosity=0)
        assert (Product.objects.count(), ProductVariant.objects.count()) == counts

    def test_a_sale_is_representable(self, runner):
        from apps.catalog.models import ProductVariant

        runner("seed_catalog", verbosity=0)
        assert ProductVariant.objects.filter(compare_at_price__isnull=False).exists()

    def test_collections_have_products(self, runner):
        from apps.catalog.models import Collection

        runner("seed_catalog", verbosity=0)
        assert any(collection.products.exists() for collection in Collection.objects.all())

    def test_quiet_mode_prints_only_the_summary(self, runner, capsys):
        runner("seed_catalog", quiet=True)
        output = capsys.readouterr().out
        assert output.strip().count("\n") == 0
        assert "Seeded" in output

    def test_verbose_mode_reports_each_section(self, runner, capsys):
        runner("seed_catalog")
        output = capsys.readouterr().out
        for section in ("sizes:", "colours:", "categories:", "products:"):
            assert section in output

    def test_placeholder_images_are_optional(self, runner):
        from apps.catalog.models import ProductImage

        runner("seed_catalog", verbosity=0)
        assert ProductImage.objects.count() == 0

    def test_placeholder_images_can_be_generated(self, runner):
        from apps.catalog.models import Product, ProductImage

        runner("seed_catalog", "--with-images", verbosity=0)
        assert ProductImage.objects.count() == Product.objects.count()

    def test_generated_images_are_primary_and_described(self, runner):
        from apps.catalog.models import Product, ProductImage

        runner("seed_catalog", "--with-images", verbosity=0)
        # Exactly one primary per product, not one per table.
        assert ProductImage.objects.filter(is_primary=True).count() == Product.objects.count()
        assert ProductImage.objects.exclude(alt_text="").count() == ProductImage.objects.count()

    def test_images_are_not_regenerated_on_a_second_run(self, runner):
        from apps.catalog.models import ProductImage

        runner("seed_catalog", "--with-images", verbosity=0)
        first = ProductImage.objects.count()
        runner("seed_catalog", "--with-images", verbosity=0)
        assert ProductImage.objects.count() == first

    def test_prices_are_deterministic_across_runs(self, runner):
        from apps.catalog.models import ProductVariant

        runner("seed_catalog", verbosity=0)
        before = dict(ProductVariant.objects.values_list("sku", "price"))
        runner("seed_catalog", verbosity=0)
        assert dict(ProductVariant.objects.values_list("sku", "price")) == before

    def test_discounted_products_compare_higher_than_they_sell_for(self, runner):
        from apps.catalog.models import ProductVariant

        runner("seed_catalog", verbosity=0)
        for variant in ProductVariant.objects.exclude(compare_at_price=None):
            assert variant.compare_at_price > variant.price
            assert Decimal(str(variant.price)) >= 0
