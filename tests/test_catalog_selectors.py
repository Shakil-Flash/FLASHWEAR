"""Tests for the catalogue read side and SKU helpers.

The selectors are where "eagerly loaded" is either true or quietly false, so a good share of these
tests assert *query counts*, not just results. That is deliberate: a page that renders the right
HTML in forty queries is a bug that only shows up when the catalogue gets large.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.catalog import selectors
from apps.catalog.models import Category, Product, ProductVariant
from apps.catalog.skus import (
    assign_unique_sku,
    build_sku,
    color_token,
    create_variant,
    size_token,
)

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# Visibility
# --------------------------------------------------------------------------------------


class TestPublishedQueryset:
    def test_only_live_products_are_returned(
        self, product, draft_product, archived_product, scheduled_product
    ):
        slugs = set(selectors.storefront_products().values_list("slug", flat=True))
        assert product.slug in slugs
        assert draft_product.slug not in slugs
        assert archived_product.slug not in slugs
        assert scheduled_product.slug not in slugs

    def test_published_is_ordered_newest_first(self, make_product):
        from django.utils import timezone

        older = make_product(
            name="Older", slug="older", published_at=timezone.now() - timezone.timedelta(days=9)
        )
        newer = make_product(name="Newer", slug="newer", published_at=timezone.now())
        assert list(selectors.storefront_products()) == [newer, older]

    def test_ordering_is_total_for_identical_timestamps(self, make_product):
        """Two products published in the same instant must not swap between pages."""
        from django.utils import timezone

        moment = timezone.now()
        first = make_product(name="Same A", slug="same-a", published_at=moment)
        second = make_product(name="Same B", slug="same-b", published_at=moment)
        assert [p.pk for p in selectors.storefront_products()] == [second.pk, first.pk]


# --------------------------------------------------------------------------------------
# Sorting
# --------------------------------------------------------------------------------------


class TestSorting:
    @pytest.fixture
    def priced(self, make_product, colour, size, other_colour, other_size, make_variant):
        cheap = make_product(
            name="Cheap", slug="cheap", published_at=_days_ago(1), is_featured=False
        )
        make_variant(cheap, color=colour, size=size, price=Decimal("10.00"), sku="CHEAP")
        dear = make_product(name="Dear", slug="dear", published_at=_days_ago(2), is_featured=True)
        make_variant(dear, color=other_colour, size=other_size, price=Decimal("90.00"), sku="DEAR")
        return cheap, dear

    def test_price_ascending_orders_by_the_cheapest_variant(self, priced):
        cheap, dear = priced
        slugs = [p.slug for p in selectors.storefront_products(sort=Product.Sort.PRICE_ASC)]
        assert slugs.index(cheap.slug) < slugs.index(dear.slug)

    def test_price_descending_is_the_reverse(self, priced):
        cheap, dear = priced
        slugs = [p.slug for p in selectors.storefront_products(sort=Product.Sort.PRICE_DESC)]
        assert slugs.index(dear.slug) < slugs.index(cheap.slug)

    def test_unpriceable_products_sort_last_ascending(self, priced, make_product):
        """A NULL price means "unknown", not "free"."""
        priceless = make_product(name="Priceless", slug="priceless", published_at=_days_ago(3))
        slugs = [p.slug for p in selectors.storefront_products(sort=Product.Sort.PRICE_ASC)]
        assert slugs[-1] == priceless.slug

    def test_featured_sort_puts_featured_first(self, priced):
        _cheap, dear = priced
        slugs = [p.slug for p in selectors.storefront_products(sort=Product.Sort.FEATURED)]
        assert slugs[0] == dear.slug

    def test_unknown_sort_falls_back_to_newest(self, priced):
        slugs = [p.slug for p in selectors.storefront_products(sort="; DROP TABLE")]
        assert slugs == [p.slug for p in selectors.storefront_products(sort=Product.Sort.NEWEST)]

    def test_resolve_sort_rejects_junk(self):
        assert selectors.resolve_sort(None) == Product.Sort.NEWEST
        assert selectors.resolve_sort("PRICE_ASC ") == Product.Sort.PRICE_ASC
        assert selectors.resolve_sort("nonsense") == Product.Sort.NEWEST

    def test_valid_sorts_are_a_frozen_allow_list(self):
        assert selectors.VALID_SORTS == frozenset(Product.Sort.values)


# --------------------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------------------


class TestCategoryFiltering:
    def test_a_department_includes_its_children(
        self, product, make_product, department, other_category
    ):
        nested = make_product(
            name="Nested", slug="nested", category=other_category, is_featured=False
        )
        slugs = set(selectors.products_in_category(department).values_list("slug", flat=True))
        assert slugs == {product.slug, nested.slug}

    def test_a_leaf_returns_only_its_own(self, product, other_category):
        assert selectors.products_in_category(other_category).count() == 0

    def test_drafts_never_appear_in_a_category(self, draft_product, category):
        assert selectors.products_in_category(category).count() == 0

    def test_category_sorting_is_honoured(self, category, make_product, colour, size, make_variant):
        first = make_product(name="First", slug="first", category=category)
        make_variant(first, color=colour, size=size, price=Decimal("80.00"), sku="CAT-FIRST")
        second = make_product(name="Second", slug="second", category=category)
        make_variant(second, color=colour, size=size, price=Decimal("20.00"), sku="CAT-SECOND")
        slugs = list(
            selectors.products_in_category(category, sort=Product.Sort.PRICE_ASC).values_list(
                "slug", flat=True
            )
        )
        assert slugs == ["second", "first"]


class TestCollectionAndBrandFiltering:
    def test_collection_products_exclude_drafts(self, product, collection, draft_product):
        collection.products.add(product, draft_product)
        assert selectors.products_in_collection(collection).count() == 1

    def test_brand_products_exclude_other_brands(self, product, other_brand, make_product):
        other = make_product(name="Northline Tee", slug="northline-tee", brand=other_brand)
        slugs = set(selectors.products_for_brand(other_brand).values_list("slug", flat=True))
        assert slugs == {other.slug}


# --------------------------------------------------------------------------------------
# Eager loading
# --------------------------------------------------------------------------------------


class TestEagerLoading:
    def test_a_grid_of_products_does_not_query_per_card(
        self, make_product, make_variant, colour, size, django_assert_num_queries
    ):
        """Seven cards must not become seven price queries."""
        from decimal import Decimal

        for index in range(6):
            extra = make_product(name=f"Extra {index}", slug=f"extra-{index}")
            make_variant(extra, color=colour, size=size, price=Decimal("5.00"), sku=f"X{index}")

        # One products query plus three prefetch batches (images, collections, variants),
        # independent of how many cards are rendered.
        with django_assert_num_queries(4):
            for card in selectors.storefront_products():
                _ = card.price_range
                _ = card.available_colors
                _ = card.primary_image
                _ = list(card.collections.all())

    def test_product_detail_prefetch_covers_everything_rendered(
        self, full_product, django_assert_num_queries
    ):
        """The first pass fetches; every pass after it must be free."""

        def touch(detail):
            for relation in (
                "materials",
                "collections",
                "tags",
                "variants",
                "images",
            ):
                _ = list(getattr(detail, relation).all())
            _ = detail.price_range
            _ = detail.available_colors
            _ = detail.available_sizes
            _ = detail.primary_image

        queryset = selectors.product_detail_queryset().published()

        with django_assert_num_queries(6) as first_pass:
            detail = queryset.get(pk=full_product.pk)
            touch(detail)

        # 1 for the product plus 5 prefetch batches (materials, collections, tags, images,
        # variants). Nothing per-relation per-card.
        assert len(first_pass.captured_queries) == 6

        with django_assert_num_queries(0):
            touch(detail)

    def test_related_products_are_a_single_extra_query(
        self, product, make_product, django_assert_num_queries
    ):
        """The "you may also like" strip is merchandising: three queries, then nothing.

        Phase 19 dropped the collections prefetch (the product card never reads it), so the
        strip costs rows + images + variants and nothing more.
        """
        for index in range(3):
            make_product(name=f"Related {index}", slug=f"related-{index}", brand=product.brand)

        with django_assert_num_queries(3):
            cards = list(selectors.related_products(product))
            assert len(cards) == 3

        with django_assert_num_queries(0):
            for card in cards:
                _ = card.price_range
                _ = card.available_colors
                _ = card.primary_image

    def test_related_products_exclude_the_product_itself(self, product):
        assert product not in selectors.related_products(product)

    def test_related_products_are_distinct_when_category_and_both_match(
        self, product, make_product
    ):
        """A product matching on both category and brand must appear once, not twice."""
        twin = make_product(
            name="Twin", slug="twin", category=product.category, brand=product.brand
        )
        slugs = [card.slug for card in selectors.related_products(product)]
        assert slugs.count(twin.slug) == 1


class TestHomepageSelectors:
    def test_featured_strip_is_capped_and_newest_first(self, make_product):
        for index in range(5):
            make_product(
                name=f"Feat {index}",
                slug=f"feat-{index}",
                is_featured=True,
                published_at=_days_ago(index + 1),
            )
        featured = selectors.homepage_featured(limit=3)
        assert len(featured) == 3
        assert featured[0].slug == "feat-0"

    def test_featured_strip_excludes_unflagged_products(self, product):
        assert selectors.homepage_featured() == []

    def test_new_arrivals_respect_the_new_flag(self, product, make_product):
        product.is_new = True
        product.save()
        old = make_product(name="Old", slug="old")
        assert [p.slug for p in selectors.homepage_new_arrivals()] == [product.slug]
        assert old.slug not in [p.slug for p in selectors.homepage_new_arrivals()]

    def test_storefront_categories_are_roots_only(self, category, department, other_category):
        slugs = set(selectors.storefront_categories().values_list("slug", flat=True))
        assert slugs == {"men"}

    def test_storefront_categories_limit_is_applied(self, department, db):
        for index in range(4):
            Category.objects.create(name=f"Dept {index}", slug=f"dept-{index}", display_order=index)
        assert selectors.storefront_categories(limit=2).count() == 2

    def test_active_brands_need_a_published_product(
        self, product, brand, other_brand, draft_product
    ):
        draft_product.brand = other_brand
        draft_product.save()
        slugs = set(selectors.active_brands().values_list("slug", flat=True))
        assert slugs == {brand.slug}


# --------------------------------------------------------------------------------------
# Taxonomy counts
# --------------------------------------------------------------------------------------


class TestCategoryCounts:
    def test_counts_roll_up_the_subtree(self, product, other_category, department):
        counts = selectors.published_category_counts()
        # One product in the leaf, none in the sibling leaf, one in the parent when it counts it.
        assert counts[product.category_id] == 1
        assert counts[other_category.pk] == 0
        assert counts[department.pk] == 1

    def test_drafts_are_not_counted(self, draft_product, category):
        assert selectors.published_category_counts()[category.pk] == 0

    def test_annotating_attaches_both_numbers(self, category):
        [annotated] = selectors.with_category_counts(Category.objects.filter(pk=category.pk))
        assert annotated.published_product_count == 0
        assert annotated.published_child_count == 0

    def test_child_count_only_counts_active_children(
        self, category, other_category, inactive_category
    ):
        """Two active children (T-Shirts, Hoodies) and one inactive (Archive)."""
        [annotated] = selectors.with_category_counts(Category.objects.filter(pk=category.parent_id))
        assert annotated.published_child_count == 2

    def test_with_category_counts_returns_a_list(self, department, category):
        result = selectors.with_category_counts(Category.objects.filter(parent__isnull=True))
        assert isinstance(result, list)
        assert result[0].slug == "men"


# --------------------------------------------------------------------------------------
# SKUs
# --------------------------------------------------------------------------------------


class TestSkuConstruction:
    def test_sku_is_deterministic(self, product, colour, size):
        first = build_sku(product=product, color=colour, size=size)
        second = build_sku(product=product, color=colour, size=size)
        assert first == second == "FW-0001-BLK-M"

    def test_sku_uses_retail_colour_abbreviations(self, product, colour, size):
        assert color_token(colour) == "BLK"
        assert size_token(size) == "M"

    def test_sku_works_without_options(self, product):
        assert build_sku(product=product) == "FW-0001-NA-NA"

    def test_build_sku_requires_a_saved_product(self, make_product, db, category, brand):
        from apps.catalog.models import Product

        unsaved = Product(name="Draft", slug="unsaved", category=category, brand=brand)
        with pytest.raises(ValueError):
            build_sku(product=unsaved)

    def test_different_options_produce_different_skus(self, product, colour, other_colour, size):
        assert build_sku(product=product, color=colour, size=size) != build_sku(
            product=product, color=other_colour, size=size
        )


class TestCreateVariant:
    def test_generated_sku_is_used_when_none_is_given(self, make_product, colour, size):
        product = make_product(name="Generated", slug="generated")
        variant = create_variant(product=product, price=Decimal("10.00"), color=colour, size=size)
        assert variant.sku == "FW-0001-BLK-M"

    def test_an_explicit_sku_is_honoured(self, make_product, colour, size):
        product = make_product(name="Explicit", slug="explicit")
        variant = create_variant(
            product=product, price=Decimal("10.00"), color=colour, size=size, sku="fw-custom-01"
        )
        assert variant.sku == "FW-CUSTOM-01"

    def test_an_explicit_duplicate_sku_raises_rather_than_being_rewritten(self, product):
        """An operator who typed a value needs to be told it is taken."""
        with pytest.raises(ValidationError) as error:
            create_variant(product=product, price=Decimal("10.00"), sku="FW-TEE-BLK-M")
        assert "sku" in error.value.message_dict

    def test_a_blank_explicit_sku_raises(self, product):
        with pytest.raises(ValidationError):
            create_variant(product=product, price=Decimal("10.00"), sku="   ")

    def test_assign_unique_sku_suffixes_a_generated_collision(self, product):
        first = assign_unique_sku(product=product)
        ProductVariant.objects.filter(pk=product.variants.first().pk).update(sku=first)
        second = assign_unique_sku(product=product)
        assert second != first
        assert second.endswith("-2")


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _days_ago(days: int):
    from django.utils import timezone

    return timezone.now() - timezone.timedelta(days=days)
