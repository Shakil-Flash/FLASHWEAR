"""Phase 37: Search Experience 2.0 regression and integration tests."""

from decimal import Decimal
import pytest
from django.urls import reverse

from apps.analytics.models import Event
from apps.catalog.models import (
    Brand,
    Category,
    Color,
    Fit,
    Material,
    Product,
    ProductTag,
    ProductVariant,
    Size,
)
from apps.catalog import selectors
from apps.catalog.templatetags.catalog_extras import (
    clear_filters_url,
    remove_filter_url,
    toggle_filter_url,
)
from apps.inventory.models import Stock

pytestmark = pytest.mark.django_db


@pytest.fixture
def fashion_catalog(db, make_product, department, brand):
    """Sets up a rich fashion catalogue for testing search intents."""
    hoodies_cat = Category.objects.create(
        name="Hoodies", slug="fashion-hoodies", parent=department, display_order=10
    )
    tees_cat = Category.objects.create(
        name="T-Shirts", slug="fashion-tees", parent=department, display_order=20
    )

    brand_flash = brand
    brand_sub = Brand.objects.create(name="Subversive", slug="subversive")

    fit_oversized = Fit.objects.create(name="Oversized", slug="oversized")
    fit_regular = Fit.objects.create(name="Regular", slug="regular")

    mat_cotton = Material.objects.create(name="Heavy Cotton", slug="heavy-cotton")
    mat_fleece = Material.objects.create(name="Brushed Fleece", slug="brushed-fleece")

    color_black = Color.objects.create(
        name="Midnight Black", slug="midnight-black", hex_code="#000000"
    )
    color_white = Color.objects.create(
        name="Bone White", slug="bone-white", hex_code="#FFFFFF"
    )

    size_l = Size.objects.create(
        name="Large", slug="large", code="L", size_type=Size.SizeType.CLOTHING
    )
    size_m = Size.objects.create(
        name="Medium", slug="medium", code="M", size_type=Size.SizeType.CLOTHING
    )

    tag_streetwear = ProductTag.objects.create(name="Streetwear", slug="streetwear")
    tag_vintage = ProductTag.objects.create(name="Vintage", slug="vintage")

    # Product 1: Black Oversized Hoodie
    p_hoodie = make_product(
        name="Cyber Heavyweight Hoodie",
        slug="cyber-heavyweight-hoodie",
        category=hoodies_cat,
        brand=brand_flash,
        fit=fit_oversized,
        description="A dark luxury oversized streetwear fleece hoodie.",
    )
    p_hoodie.materials.add(mat_fleece)
    p_hoodie.tags.add(tag_streetwear)
    var_hoodie = ProductVariant.objects.create(
        product=p_hoodie,
        sku="HOOD-BLK-L",
        color=color_black,
        size=size_l,
        price=Decimal("120.00"),
        is_active=True,
    )
    Stock.objects.create(variant=var_hoodie, on_hand=15, reserved=2)

    # Product 2: White Regular Tee
    p_tee = make_product(
        name="Essential Minimal Tee",
        slug="essential-minimal-tee",
        category=tees_cat,
        brand=brand_sub,
        fit=fit_regular,
        description="Clean white vintage-washed everyday tee.",
    )
    p_tee.materials.add(mat_cotton)
    p_tee.tags.add(tag_vintage)
    var_tee = ProductVariant.objects.create(
        product=p_tee,
        sku="TEE-WHT-M",
        color=color_white,
        size=size_m,
        price=Decimal("45.00"),
        is_active=True,
    )
    # Zero stock for in-stock filtering test
    Stock.objects.create(variant=var_tee, on_hand=0, reserved=0)

    return {
        "hoodie": p_hoodie,
        "tee": p_tee,
        "hoodies_cat": hoodies_cat,
        "tees_cat": tees_cat,
        "color_black": color_black,
        "color_white": color_white,
        "size_l": size_l,
        "size_m": size_m,
        "fit_oversized": fit_oversized,
        "brand_flash": brand_flash,
        "brand_sub": brand_sub,
    }


class TestFashionSearchMatching:
    def test_search_matches_by_product_name(self, fashion_catalog):
        qs = selectors.storefront_products()
        results = selectors.apply_search(qs, "Cyber Heavyweight")
        assert list(results) == [fashion_catalog["hoodie"]]

    def test_search_matches_by_category_name(self, fashion_catalog):
        qs = selectors.storefront_products()
        results = selectors.apply_search(qs, "Hoodies")
        assert list(results) == [fashion_catalog["hoodie"]]

    def test_search_matches_by_brand_name(self, fashion_catalog):
        qs = selectors.storefront_products()
        results = selectors.apply_search(qs, "Subversive")
        assert list(results) == [fashion_catalog["tee"]]

    def test_search_matches_by_fit_and_material(self, fashion_catalog):
        qs = selectors.storefront_products()
        # Fit match
        results_fit = selectors.apply_search(qs, "Oversized")
        assert list(results_fit) == [fashion_catalog["hoodie"]]

        # Material match
        results_mat = selectors.apply_search(qs, "Fleece")
        assert list(results_mat) == [fashion_catalog["hoodie"]]

    def test_search_matches_by_color_and_size(self, fashion_catalog):
        qs = selectors.storefront_products()
        # Variant color match
        results_color = selectors.apply_search(qs, "Midnight Black")
        assert list(results_color) == [fashion_catalog["hoodie"]]

    def test_search_matches_by_style_tag(self, fashion_catalog):
        qs = selectors.storefront_products()
        results_tag = selectors.apply_search(qs, "Streetwear")
        assert list(results_tag) == [fashion_catalog["hoodie"]]

    def test_search_fashion_intent_multiword_conjunction(self, fashion_catalog):
        # "black oversized hoodie" matches color, fit, and category
        qs = selectors.storefront_products()
        results = selectors.apply_search(qs, "black oversized hoodie")
        assert list(results) == [fashion_catalog["hoodie"]]


class TestSearchSortingAndRelevance:
    def test_resolve_sort_defaults_to_relevance_when_query_present(self):
        assert selectors.resolve_sort(None, has_query=True) == "relevance"
        assert selectors.resolve_sort("", has_query=True) == "relevance"
        assert selectors.resolve_sort(
            Product.Sort.PRICE_ASC, has_query=True
        ) == Product.Sort.PRICE_ASC

    def test_resolve_sort_defaults_to_newest_when_browsing(self):
        assert selectors.resolve_sort(None, has_query=False) == Product.Sort.NEWEST
        assert selectors.resolve_sort("", has_query=False) == Product.Sort.NEWEST
        assert selectors.resolve_sort("nonsense", has_query=False) == Product.Sort.NEWEST

    def test_relevance_sort_boosts_exact_phrase_and_title_matches(
        self, fashion_catalog, make_product
    ):
        # Create a second product that only mentions hoodie in description
        p_other = make_product(
            name="Urban Accessories Belt",
            slug="urban-accessories-belt",
            category=fashion_catalog["tees_cat"],
            brand=fashion_catalog["brand_flash"],
            description="Looks good when styled with an oversized hoodie.",
        )
        qs = selectors.storefront_products()
        qs = selectors.apply_search(qs, "hoodie")
        sorted_qs = selectors.apply_sorting(qs, "relevance")
        product_list = list(sorted_qs)
        assert len(product_list) == 2
        # p_hoodie matches category name + description + title keywords, so it ranks first
        assert product_list[0] == fashion_catalog["hoodie"]
        assert product_list[1] == p_other


class TestSearchFiltersAndInStock:
    def test_in_stock_filter_excludes_out_of_stock_products(self, client, fashion_catalog):
        url = reverse("catalog:product-search")
        # Without in_stock, both items match broad search or empty query
        res_all = client.get(url, {"q": "tee"})
        assert fashion_catalog["tee"].name.encode() in res_all.content

        # With in_stock=1, tee has 0 stock so it should be excluded
        res_stock = client.get(url, {"q": "tee", "in_stock": "1"})
        assert fashion_catalog["tee"].name.encode() not in res_stock.content

    def test_price_range_filtering_with_search(self, client, fashion_catalog):
        url = reverse("catalog:product-search")
        # Min price 100 filters out the 45.00 tee
        res = client.get(url, {"q": "hoodie", "min_price": "100"})
        assert res.status_code == 200
        assert fashion_catalog["hoodie"].name.encode() in res.content


class TestSearchTemplatetagsUrlState:
    def test_clear_filters_url_preserves_search_query(self, rf):
        request = rf.get("/search/?q=hoodie&category=hoodies&in_stock=1&sort=relevance")
        context = {"request": request}
        url = clear_filters_url(context)
        assert "q=hoodie" in url
        assert "category=" not in url
        assert "in_stock=" not in url

    def test_toggle_filter_url_adds_and_removes_values(self, rf):
        request = rf.get("/search/?q=hoodie&size=M")
        context = {"request": request}
        # Toggling 'L' adds it
        url_add = toggle_filter_url(context, "size", "L")
        assert "size=m%2Cl" in url_add or "size=M%2CL" in url_add or "l" in url_add

        # Toggling 'M' removes it
        url_remove = toggle_filter_url(context, "size", "M")
        assert "size=" not in url_remove or "size=&" in url_remove

    def test_remove_filter_url(self, rf):
        request = rf.get("/search/?q=hoodie&category=hoodies&in_stock=1")
        context = {"request": request}
        url = remove_filter_url(context, "category")
        assert "category=" not in url
        assert "q=hoodie" in url


class TestSearchSuggestionsApi:
    def test_suggestions_api_returns_enriched_results(self, client, fashion_catalog):
        res = client.get("/api/v1/products/suggestions/?q=hoodie")
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) > 0

        # Should contain category suggestion and product suggestion
        types = {item["type"] for item in data}
        assert "category" in types or "product" in types

        # Check badges and product metadata
        product_items = [i for i in data if i["type"] == "product"]
        if product_items:
            p = product_items[0]
            assert "name" in p
            assert "url" in p
            assert "badge" in p


class TestSpellingSuggestionsAndZeroResults:
    def test_spelling_suggestions_for_typos(self):
        suggestions = selectors.get_spelling_suggestions("hoodei")
        assert "hoodie" in suggestions

    def test_zero_results_page_displays_helpful_guidance(self, client):
        url = reverse("catalog:product-search")
        res = client.get(url, {"q": "nonexistentgarment123"})
        assert res.status_code == 200
        content = res.content.decode()
        assert "No garments found for “nonexistentgarment123”" in content
        assert "Clear all filters" in content


class TestSearchAnalytics:
    def test_product_search_and_filter_events_emitted(self, client, fashion_catalog):
        url = reverse("catalog:product-search")
        res = client.get(url, {"q": "hoodie", "in_stock": "1"})
        assert res.status_code == 200

        search_events = Event.objects.filter(name="product_search")
        assert search_events.exists()
        ev = search_events.latest("created_at")
        assert ev.metadata.get("query") == "hoodie"
        assert ev.metadata.get("zero_results") is False

        filter_events = Event.objects.filter(name="filter_used")
        assert filter_events.exists()


class TestSearchLandingAndErrorHandling:
    def test_empty_search_landing_page(self, client):
        url = reverse("catalog:product-search")
        res = client.get(url)
        assert res.status_code == 200
        assert b"Search Catalogue" in res.content
        assert b"Search FLASHWEAR fashion across all silhouettes" in res.content
