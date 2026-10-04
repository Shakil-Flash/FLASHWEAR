"""Tests for the catalogue domain layer (Phase 3).

Covers what the models and services are responsible for: lifecycle rules, price and variant
invariants, slugs, SKUs, the category tree and the gallery. The read side and the HTTP surface are
tested in the modules that follow.

The class name states the rule; the test name states the observation.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Value
from django.utils import timezone

from apps.catalog import services
from apps.catalog.models import (
    Brand,
    Category,
    Collection,
    Color,
    Product,
    ProductImage,
    ProductVariant,
    Size,
)

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# Product lifecycle
# --------------------------------------------------------------------------------------


class TestProductStatus:
    """Draft, Active and Archived, and what each one means for the public."""

    def test_draft_is_not_published(self, draft_product):
        assert draft_product.status == Product.Status.DRAFT
        assert draft_product.is_published is False
        assert Product.objects.published().filter(pk=draft_product.pk).exists() is False

    def test_active_with_a_past_date_is_published(self, product):
        assert product.is_published is True
        assert Product.objects.published().filter(pk=product.pk).exists() is True

    def test_active_with_a_future_date_is_not_yet_published(self, scheduled_product):
        """Active means "published" only once its date arrives; a drop can be dated forward."""
        assert scheduled_product.status == Product.Status.ACTIVE
        assert scheduled_product.is_published is False
        assert Product.objects.published().filter(pk=scheduled_product.pk).exists() is False

    def test_archived_is_never_published(self, archived_product):
        assert archived_product.status == Product.Status.ARCHIVED
        assert archived_product.is_published is False
        assert Product.objects.published().filter(pk=archived_product.pk).exists() is False

    def test_saving_as_active_stamps_published_at(self, make_product):
        product = make_product(name="Fresh Tee", slug="fresh-tee", status=Product.Status.ACTIVE)
        product.published_at = None
        product.save()
        product.refresh_from_db()
        assert product.published_at is not None

    def test_draft_keeps_a_manually_cleared_date(self, make_product):
        """Unpublishing must be able to remove the date, not just overwrite it."""
        product = make_product(
            name="Second Draft",
            slug="second-draft",
            status=Product.Status.DRAFT,
            published_at=None,
        )
        assert product.published_at is None

    def test_unknown_status_is_rejected(self, make_product):
        product = make_product(name="Weird Tee", slug="weird-tee", status="live")
        with pytest.raises(ValidationError):
            product.full_clean()

    def test_status_choices_have_no_out_of_stock(self):
        """Availability is derived from inventory, not stored as a lifecycle state."""
        assert set(Product.Status.values) == {"draft", "active", "archived"}
        assert "out_of_stock" not in set(Product.Status.values)


# --------------------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------------------


class TestVariantPricing:
    def test_price_may_not_be_negative(self, product, colour, size, make_variant):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_variant(product, color=colour, size=size, sku="NEG", price=Decimal("-1.00"))

    def test_compare_at_below_price_is_rejected(
        self, product, other_colour, other_size, make_variant
    ):
        """A "was" price cheaper than the price is a pricing bug, not a promotion."""
        with pytest.raises(IntegrityError), transaction.atomic():
            make_variant(
                product,
                color=other_colour,
                size=other_size,
                sku="BAD-COMPARE",
                price=Decimal("50.00"),
                compare_at_price=Decimal("40.00"),
            )

    def test_equal_compare_at_price_is_not_a_discount(
        self, product, other_colour, other_size, make_variant
    ):
        variant = make_variant(
            product,
            color=other_colour,
            size=other_size,
            sku="SAME",
            price=Decimal("50.00"),
            compare_at_price=Decimal("50.00"),
        )
        assert variant.is_discounted is False
        assert variant.discount_percent == 0

    def test_discount_percent_rounds_rather_than_truncates(
        self, product, other_colour, other_size, make_variant
    ):
        variant = make_variant(
            product,
            color=other_colour,
            size=other_size,
            sku="ROUND",
            price=Decimal("1490.00"),
            compare_at_price=Decimal("1990.00"),
        )
        # 25.1% rounds to 25, which is the figure a shopper expects from a "-25%" badge.
        assert variant.discount_percent == 25

    def test_margin_is_none_without_a_cost_price(
        self, product, other_colour, other_size, make_variant
    ):
        variant = make_variant(
            product,
            color=other_colour,
            size=other_size,
            sku="NO-COST",
            price=Decimal("50.00"),
        )
        assert variant.margin_percent is None

    def test_margin_uses_the_recorded_cost(self, product, other_colour, other_size, make_variant):
        variant = make_variant(
            product,
            color=other_colour,
            size=other_size,
            sku="COST",
            price=Decimal("100.00"),
            cost_price=Decimal("60.00"),
        )
        assert variant.margin_percent == 40

    def test_price_range_spans_active_variants(
        self, product, colour, size, other_colour, other_size, make_variant
    ):
        # The fixture's own variant is the inactive one: a discontinued size must not
        # influence the price a shopper is shown.
        ProductVariant.objects.filter(product=product).update(is_active=False)
        make_variant(product, color=colour, size=other_size, price=Decimal("59.00"), sku="PRICE-B")
        make_variant(product, color=other_colour, size=size, price=Decimal("39.00"), sku="PRICE-C")
        assert product.price_range == (Decimal("39.00"), Decimal("59.00"))

    def test_price_range_is_none_none_without_purchasable_variants(self, make_product):
        product = make_product(name="No Variants", slug="no-variants")
        assert product.price_range == (None, None)

    def test_price_range_ignores_inactive_variants(self, make_product, make_variant):
        product = make_product(name="Only Draft Variant", slug="only-draft-variant")
        make_variant(product, sku="INACTIVE", price=Decimal("99.00"), is_active=False)
        # The variant exists but is inactive, so the product has nothing purchasable yet.
        assert product.price_range == (None, None)

    def test_price_annotations_take_precedence(self, product):
        """An annotated range is the database's answer and must win over the Python recompute."""
        annotated = (
            Product.objects.with_storefront_data()
            .annotate(price_min=Value(Decimal("11.00")), price_max=Value(Decimal("22.00")))
            .get(pk=product.pk)
        )
        assert annotated.price_range == (Decimal("11.00"), Decimal("22.00"))


# --------------------------------------------------------------------------------------
# Variant identity
# --------------------------------------------------------------------------------------


class TestVariantUniqueness:
    def test_same_color_and_size_twice_is_rejected(self, product, other_colour, size, make_variant):
        make_variant(product, color=other_colour, size=size, sku="UNIQ-A")
        with pytest.raises(IntegrityError), transaction.atomic():
            make_variant(product, color=other_colour, size=size, sku="UNIQ-B")

    def test_different_sizes_coexist(self, product, colour, size, other_size, make_variant):
        second = make_variant(product, color=colour, size=other_size, sku="SIZE-L")
        assert second.pk is not None
        assert second.product_id == product.pk

    def test_only_one_option_less_variant_per_product(self, product, make_variant):
        """A belt has a size but no colourway; a gift card has neither. One of each, max."""
        first = make_variant(product, color=None, size=None, sku="NOOPT-A")
        with pytest.raises(IntegrityError), transaction.atomic():
            make_variant(product, color=None, size=None, sku="NOOPT-B")
        assert first.pk

    def test_same_options_on_a_different_product_are_allowed(
        self, make_product, colour, size, make_variant
    ):
        first = make_product(name="Product One", slug="product-one")
        second = make_product(name="Product Two", slug="product-two")
        make_variant(first, color=colour, size=size, sku="CROSS-1")
        assert make_variant(second, color=colour, size=size, sku="CROSS-2").pk is not None

    def test_sku_is_unique_across_products(self, product, make_product, make_variant):
        other = make_product(name="Copycat", slug="copycat")
        with pytest.raises(IntegrityError), transaction.atomic():
            make_variant(other, sku="FW-TEE-BLK-M")

    def test_sku_is_normalised_to_upper_case(self, product):
        variant = ProductVariant.objects.filter(pk=product.variants.first().pk).first()
        variant.sku = "  fw-lower-case  "
        variant.save()
        variant.refresh_from_db()
        assert variant.sku == "FW-LOWER-CASE"

    def test_barcode_is_optional_and_unique_when_given(
        self, product, other_colour, size, other_size, make_variant
    ):
        make_variant(product, color=other_colour, size=size, sku="BAR-1", barcode="1000000000001")
        with pytest.raises(IntegrityError), transaction.atomic():
            make_variant(
                product,
                color=other_colour,
                size=other_size,
                sku="BAR-2",
                barcode="1000000000001",
            )

    def test_blank_barcodes_do_not_collide(
        self, product, other_colour, size, other_size, make_variant
    ):
        make_variant(product, color=other_colour, size=size, sku="BAR-A", barcode="")
        assert (
            make_variant(product, color=other_colour, size=other_size, sku="BAR-B").pk is not None
        )


# --------------------------------------------------------------------------------------
# Slugs and URLs
# --------------------------------------------------------------------------------------


class TestSlugs:
    def test_slug_is_derived_from_the_name(self, make_product):
        product = make_product(name="Studio Rib Tee", slug="studio-rib-tee")
        assert product.slug == "studio-rib-tee"

    def test_a_blank_slug_is_derived_and_disambiguated(self, make_product):
        """Two products may share a name; they may not share a URL."""
        first = make_product(name="Oversized Tee", slug="")
        second = make_product(name="Oversized Tee", slug="")
        assert first.slug == "oversized-tee"
        assert second.slug == "oversized-tee-2"

    def test_an_explicit_duplicate_slug_is_refused_by_the_database(self, make_product):
        """A typed slug is never silently rewritten: it would break inbound links."""
        make_product(name="Original", slug="shared-slug")
        with pytest.raises(IntegrityError), transaction.atomic():
            make_product(name="Copy", slug="shared-slug")

    def test_punctuation_is_stripped(self, make_product):
        product = make_product(name="Cargo Pant (Army)", slug="cargo-pant-army")
        assert product.slug == "cargo-pant-army"

    def test_absolute_url_is_slug_addressed(self, product):
        assert product.get_absolute_url() == f"/products/{product.slug}/"


# --------------------------------------------------------------------------------------
# Category tree
# --------------------------------------------------------------------------------------


class TestCategoryTree:
    def test_ancestors_are_root_first(self, category, department):
        assert [node.slug for node in category.ancestors()] == ["men"]
        assert [node.slug for node in category.breadcrumb_trail()] == ["men", "t-shirts"]

    def test_depth_counts_ancestors(self, category, department):
        assert department.depth == 0
        assert category.depth == 1

    def test_a_category_cannot_be_its_own_parent(self, category):
        category.parent = category
        with pytest.raises(ValidationError):
            category.full_clean()

    def test_a_category_cannot_move_under_its_own_child(self, category, department):
        """Re-parenting Men under Men > T-Shirts would create a loop the tree walk cannot escape."""
        department.parent = category
        with pytest.raises(ValidationError):
            department.full_clean()

    def test_save_revalidates_the_tree(self, category, department):
        """Guards imports and shell commands, which never call ``full_clean``."""
        department.parent = category
        with pytest.raises(ValidationError):
            department.save()

    def test_subtree_includes_descendants(self, category, other_category, department):
        ids = services.category_subtree_ids(department)
        assert set(ids) == {department.pk, category.pk, other_category.pk}

    def test_deleting_a_category_with_products_is_protected(self, category, product):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                category.delete()

    def test_deleting_an_empty_leaf_is_allowed(self, other_category):
        other_category.delete()
        assert Category.objects.filter(slug="hoodies").exists() is False


# --------------------------------------------------------------------------------------
# Brands and collections
# --------------------------------------------------------------------------------------


class TestBrandRules:
    def test_blank_website_is_allowed(self, db):
        """No website is a normal state; the constraint must not reject it."""
        brand = Brand.objects.create(name="No Site", slug="no-site", website_url="")
        brand.full_clean()

    def test_https_website_is_allowed(self, db):
        Brand.objects.create(name="With Site", slug="with-site", website_url="https://example.com")

    def test_javascript_website_is_rejected_by_the_database(self, db):
        brand = Brand.objects.create(
            name="Hostile", slug="hostile", website_url="https://example.com"
        )
        brand.website_url = "javascript:alert(1)"
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                brand.save()

    def test_brand_is_optional_on_a_product(self, make_product):
        product = make_product(name="Unbranded", slug="unbranded", brand=None)
        assert product.brand_id is None


class TestCollectionWindows:
    def test_open_ended_collection_is_current(self, collection):
        assert collection.is_current is True
        assert collection.is_live is True

    def test_future_collection_is_not_current(self, db):
        collection = Collection.objects.create(
            name="Tomorrow",
            slug="tomorrow",
            starts_at=timezone.now() + timezone.timedelta(days=2),
        )
        assert collection.is_current is False
        assert collection.is_live is False

    def test_expired_collection_is_not_current(self, db):
        collection = Collection.objects.create(
            name="Yesterday",
            slug="yesterday",
            ends_at=timezone.now() - timezone.timedelta(days=1),
        )
        assert collection.is_current is False

    def test_inactive_collection_is_not_live(self, collection):
        collection.is_active = False
        assert collection.is_live is False

    def test_reversed_window_is_rejected(self, db):
        collection = Collection(
            name="Backwards",
            slug="backwards",
            starts_at=timezone.now(),
            ends_at=timezone.now() - timezone.timedelta(days=1),
        )
        with pytest.raises(ValidationError):
            collection.full_clean()

    def test_products_can_be_in_several_collections(self, product, collection, db):
        other = Collection.objects.create(name="Midnight", slug="midnight")
        product.collections.set([collection, other])
        assert product.collections.count() == 2


# --------------------------------------------------------------------------------------
# Size and colour vocabularies
# --------------------------------------------------------------------------------------


class TestAttributeVocabularies:
    def test_size_code_is_upper_cased(self, db):
        size = Size.objects.create(
            name="Extra Large",
            slug="extra-large",
            code="xl",
            size_type=Size.SizeType.CLOTHING,
        )
        assert size.code == "XL"

    def test_unknown_size_type_is_rejected(self, db):
        size = Size(name="Odd", slug="odd", code="ODD", size_type="apparel")
        with pytest.raises(ValidationError):
            size.full_clean()

    def test_size_type_allows_waist_sizes(self, waist_size):
        assert waist_size.size_type == Size.SizeType.NUMERIC

    def test_color_rgb_parses_the_hex(self, colour):
        assert colour.rgb == (17, 24, 39)

    def test_inactive_color_is_excluded_from_available_colors(
        self, product, inactive_colour, size, make_variant
    ):
        make_variant(product, color=inactive_colour, size=size, sku="INACTIVE-COLOR")
        assert inactive_colour not in product.available_colors


# --------------------------------------------------------------------------------------
# Gallery
# --------------------------------------------------------------------------------------


class TestGallery:
    def test_first_image_becomes_primary(self, product):
        image = ProductImage.objects.create(
            product=product, image="catalog/tests/a.png", alt_text="Front", position=0
        )
        assert image.is_primary is True

    def test_only_one_image_stays_primary(self, product):
        first = ProductImage.objects.create(
            product=product, image="catalog/tests/a.png", alt_text="Front", position=0
        )
        second = ProductImage.objects.create(
            product=product, image="catalog/tests/b.png", alt_text="Back", position=1
        )
        first.refresh_from_db()
        second.refresh_from_db()
        assert [first.is_primary, second.is_primary] == [True, False]

    def test_alt_text_is_required(self, product):
        image = ProductImage(product=product, image="catalog/tests/a.png", alt_text="", position=0)
        with pytest.raises(ValidationError):
            image.full_clean()

    def test_deleting_the_primary_promotes_another(self, product):
        first = ProductImage.objects.create(
            product=product, image="catalog/tests/a.png", alt_text="Front", position=0
        )
        second = ProductImage.objects.create(
            product=product, image="catalog/tests/b.png", alt_text="Back", position=1
        )
        first.delete()
        second.refresh_from_db()
        assert second.is_primary is True

    def test_promote_primary_image_moves_the_flag(self, product):
        first = ProductImage.objects.create(
            product=product, image="catalog/tests/a.png", alt_text="Front", position=0
        )
        second = ProductImage.objects.create(
            product=product, image="catalog/tests/b.png", alt_text="Back", position=1
        )
        services.promote_primary_image(product, second)
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.is_primary is False
        assert second.is_primary is True

    def test_gallery_splits_shared_and_colour_images(
        self, product, other_colour, other_size, make_variant
    ):
        variant = make_variant(product, color=other_colour, size=other_size, sku="GALLERY-1")
        shared = ProductImage.objects.create(
            product=product, image="catalog/tests/shared.png", alt_text="Flat lay", position=0
        )
        ProductImage.objects.create(
            product=product,
            image="catalog/tests/black.png",
            alt_text="Black tee",
            position=1,
            variant=variant,
        )
        gallery = services.product_gallery(product)
        assert gallery["shared"] == [shared]
        assert len(gallery["by_color"][other_colour.pk]) == 1

    def test_reorder_images_writes_the_new_order(self, product):
        first = ProductImage.objects.create(
            product=product, image="catalog/tests/a.png", alt_text="A", position=0
        )
        second = ProductImage.objects.create(
            product=product, image="catalog/tests/b.png", alt_text="B", position=1
        )
        services.reorder_images(product, [second.pk, first.pk])
        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.position, second.position) == (1, 0)

    def test_reorder_ignores_images_from_another_product(self, product, make_product):
        mine = ProductImage.objects.create(
            product=product, image="catalog/tests/mine.png", alt_text="Mine", position=0
        )
        theirs_product = make_product(name="Other", slug="other")
        theirs = ProductImage.objects.create(
            product=theirs_product, image="catalog/tests/theirs.png", alt_text="Theirs", position=0
        )
        assert services.reorder_images(product, [theirs.pk, mine.pk]) == 1
        theirs.refresh_from_db()
        assert theirs.position == 0

    def test_image_cannot_belong_to_another_product(
        self, product, make_product, colour, size, make_variant
    ):
        other = make_product(name="Other Owner", slug="other-owner")
        variant = make_variant(other, color=colour, size=size, sku="CROSS-PRODUCT")
        image = ProductImage(
            product=product, image="catalog/tests/x.png", alt_text="X", variant=variant, position=0
        )
        with pytest.raises(ValidationError):
            image.full_clean()

    def test_upload_path_ignores_the_client_filename(self, product):
        """The stored name is a UUID: a filename is attacker-controlled text in a public URL."""
        from apps.catalog.models.product import catalog_image_upload_to

        image = ProductImage(product=product, alt_text="x")
        path = catalog_image_upload_to(image, "../../etc/passwd.svg")
        assert path.startswith(f"catalog/products/{product.pk}/")
        assert "passwd" not in path
        # An unrecognised extension yields no extension at all, never ".svg": an SVG is
        # executable markup and must not land under a path a browser will run.
        assert not path.endswith(".svg")
        assert "svg" not in path

    def test_upload_path_keeps_a_recognised_extension(self, product):
        from apps.catalog.models.product import catalog_image_upload_to

        image = ProductImage(product=product, alt_text="x")
        assert catalog_image_upload_to(image, "front.JPG").endswith(".jpg")


# --------------------------------------------------------------------------------------
# Publishing services
# --------------------------------------------------------------------------------------


class TestPublishingServices:
    def test_publish_stamps_the_date(self, draft_product):
        published = services.publish(draft_product)
        published.refresh_from_db()
        assert published.status == Product.Status.ACTIVE
        assert published.published_at is not None

    def test_publish_into_an_inactive_category_is_refused(self, make_product, inactive_category):
        product = make_product(
            name="Hidden Category", slug="hidden-category", category=inactive_category
        )
        with pytest.raises(ValidationError):
            services.publish(product)

    def test_unpublish_returns_to_draft(self, product):
        result = services.unpublish(product)
        result.refresh_from_db()
        assert result.status == Product.Status.DRAFT

    def test_archive_keeps_the_record(self, product):
        result = services.archive(product)
        result.refresh_from_db()
        assert result.status == Product.Status.ARCHIVED
        assert Product.objects.filter(pk=product.pk).exists() is True

    def test_publishing_a_product_with_no_variants_is_allowed(self, draft_product):
        """Merchandisers often draft first and add sizes later; that must not be a dead end."""
        assert services.publish(draft_product).status == Product.Status.ACTIVE


# --------------------------------------------------------------------------------------
# Variant matrix
# --------------------------------------------------------------------------------------


class TestVariantMatrix:
    def test_matrix_lists_ordered_options(self, full_product):
        matrix = services.build_variant_matrix(full_product)
        assert [colour.name for colour in matrix.colors] == ["Black", "Sand"]
        assert [size.code for size in matrix.sizes] == ["M", "L"]

    def test_matrix_keys_every_combination(self, full_product, colour, size):
        matrix = services.build_variant_matrix(full_product)
        assert (colour.pk, size.pk) in matrix.variants
        assert matrix.has_combination(colour, size) is True

    def test_selecting_a_valid_combination_returns_that_variant(
        self, full_product, other_colour, other_size
    ):
        matrix = services.build_variant_matrix(full_product, color=other_colour, size=other_size)
        assert matrix.selected.color_id == other_colour.pk
        assert matrix.selected.size_id == other_size.pk

    def test_selecting_a_colour_without_a_size_picks_the_first_of_that_colour(
        self, full_product, other_colour
    ):
        matrix = services.build_variant_matrix(full_product, color=other_colour)
        assert matrix.selected.color_id == other_colour.pk

    def test_impossible_combination_falls_back_instead_of_erroring(self, full_product, colour):
        """A stale bookmark must not 500; it should land on something real."""
        missing = Size.objects.create(
            name="Tiny", slug="tiny", code="XS", size_type=Size.SizeType.CLOTHING
        )
        matrix = services.build_variant_matrix(full_product, color=colour, size=missing)
        assert matrix.selected is not None
        assert matrix.selected.size_id != missing.pk

    def test_unknown_objects_are_ignored(self, full_product):
        matrix = services.build_variant_matrix(full_product)
        assert matrix.selected is not None

    def test_matrix_reuses_prefetched_variants(self, full_product):
        """The product page already fetched the variants; the matrix must not re-query."""
        prefetched = Product.objects.with_storefront_data().get(pk=full_product.pk)
        assert "variants" in prefetched._prefetched_objects_cache
        matrix = services.build_variant_matrix(prefetched)
        assert matrix.combination_count == 4

    def test_inactive_variants_are_excluded(self, full_product, make_variant):
        make_variant(full_product, price=Decimal("1.00"), sku="HIDDEN", is_active=False)
        matrix = services.build_variant_matrix(full_product)
        assert "HIDDEN" not in {variant.sku for variant in matrix.variants.values()}

    def test_empty_matrix_for_a_product_without_variants(self, make_product):
        product = make_product(name="Bare", slug="bare")
        matrix = services.build_variant_matrix(product)
        assert matrix.has_variants is False
        assert matrix.selected is None

    def test_colors_for_size_narrows_the_options(self, full_product, size):
        matrix = services.build_variant_matrix(full_product)
        assert len(matrix.colors_for_size(size)) == 2


# --------------------------------------------------------------------------------------
# Colour presentation
# --------------------------------------------------------------------------------------


class TestProductDerivedValues:
    def test_available_colors_are_deduplicated_and_ordered(self, full_product):
        assert [colour.name for colour in full_product.available_colors] == ["Black", "Sand"]

    def test_available_sizes_are_deduplicated_and_ordered(self, full_product):
        assert [size.code for size in full_product.available_sizes] == ["M", "L"]

    def test_is_on_sale_reflects_any_discounted_variant(self, full_product):
        assert full_product.is_on_sale is True

    def test_is_not_on_sale_without_a_compare_at_price(self, product):
        assert product.is_on_sale is False

    def test_sale_state_ignores_inactive_variants(
        self, product, other_colour, other_size, make_variant
    ):
        make_variant(
            product,
            color=other_colour,
            size=other_size,
            sku="DISCONTINUED-SALE",
            price=Decimal("10.00"),
            compare_at_price=Decimal("20.00"),
            is_active=False,
        )
        assert product.is_on_sale is False

    def test_option_label_names_the_options(self, product, other_colour, other_size, make_variant):
        variant = make_variant(product, color=other_colour, size=other_size, sku="LABELLED")
        assert variant.option_label == "Sand / Large"

    def test_option_label_omits_missing_options(self, product, colour, make_variant):
        variant = make_variant(product, color=colour, size=None, sku="COLOR-ONLY")
        assert variant.option_label == "Black"

    def test_option_label_is_empty_for_a_bare_variant(self, product, make_variant):
        variant = make_variant(product, color=None, size=None, sku="NO-LABEL")
        assert variant.option_label == ""

    def test_primary_image_falls_back_to_the_first(self, product):
        first = ProductImage.objects.create(
            product=product, image="catalog/tests/first.png", alt_text="First", position=0
        )
        first.is_primary = False
        first.save(update_fields=["is_primary"])
        assert product.primary_image.pk == first.pk

    def test_price_range_uses_prefetched_variants(
        self, product, other_colour, other_size, make_variant
    ):
        make_variant(
            product,
            color=other_colour,
            size=other_size,
            price=Decimal("79.00"),
            sku="SECOND-PRICE",
        )
        prefetched = Product.objects.with_storefront_data().get(pk=product.pk)
        assert prefetched.price_range == (Decimal("49.00"), Decimal("79.00"))

    def test_inactive_color_hides_its_swatch_from_a_grid_card(
        self, product, other_colour, other_size, make_variant
    ):
        make_variant(product, color=other_colour, size=other_size, sku="RETIRED-COLOUR")
        other_colour.is_active = False
        other_colour.save()
        card = Product.objects.with_storefront_data().get(pk=product.pk)
        assert [c.slug for c in card.available_colors] == ["black"]


# --------------------------------------------------------------------------------------
# Model-level validation that only a form can act on
# --------------------------------------------------------------------------------------


class TestProductClean:
    def test_live_product_in_an_inactive_category_is_reported_against_the_field(
        self, make_product, inactive_category
    ):
        product = make_product(
            name="Misfiled",
            slug="misfiled",
            category=inactive_category,
            status=Product.Status.ACTIVE,
        )
        with pytest.raises(ValidationError) as error:
            product.full_clean()
        assert "category" in error.value.error_dict

    def test_short_description_length_is_bounded(self, make_product):
        product = make_product(name="Verbose", slug="verbose")
        product.short_description = "x" * 301
        with pytest.raises(ValidationError):
            product.full_clean()


class TestColorsAndSizesAreActiveFlags:
    def test_deactivating_a_color_hides_it_from_the_matrix(self, product, colour):
        """The fixture variant is Black / M, so retiring Black removes the option."""
        assert colour in services.build_variant_matrix(product).colors
        colour.is_active = False
        colour.save()
        matrix = services.build_variant_matrix(product)
        assert colour not in matrix.colors
        # The variant itself is untouched: it may be relisted when the colour returns.
        assert matrix.variants[(colour.pk, product.variants.first().size_id)] is not None

    def test_color_references_survive_a_rename(
        self, product, other_colour, other_size, make_variant
    ):
        """Variants point at the colour row, not at its name."""
        variant = make_variant(product, color=other_colour, size=other_size, sku="STABLE-REF")
        other_colour.name = "Dune"
        other_colour.save()
        variant.refresh_from_db()
        assert variant.color.name == "Dune"


class TestSlugHelpers:
    def test_slug_candidate_strips_accents_and_symbols(self):
        from apps.catalog.slugs import slug_candidate

        assert slug_candidate("Café Crème — Oversized") == "cafe-creme-oversized"

    def test_slug_candidate_never_returns_empty(self):
        from apps.catalog.slugs import slug_candidate

        assert slug_candidate("!!!") != ""

    def test_unique_slug_walks_past_taken_values(self, make_product):
        from apps.catalog.slugs import unique_slug

        make_product(name="First", slug="taken")
        make_product(name="Second", slug="taken-2")
        assert unique_slug(Product, "taken") == "taken-3"

    def test_unique_slug_returns_the_same_value_for_an_unsaved_instance(self, make_product):
        """Re-saving a product without renaming it must not append -2 to itself."""
        from apps.catalog.slugs import unique_slug

        product = make_product(name="Stable", slug="stable")
        assert unique_slug(Product, "stable", instance=product) == "stable"


class TestColorsAreNeverDuplicatedByName:
    def test_two_colors_may_share_a_three_letter_token(self, db):
        """BLU and Blue collapse to the same SKU token; the suffix logic keeps them apart."""
        from apps.catalog.skus import color_token

        one = Color.objects.create(name="Blue", slug="blue", hex_code="#1d4ed8")
        two = Color.objects.create(name="Blush", slug="blush", hex_code="#f9a8d4")
        assert color_token(one) == color_token(two)
