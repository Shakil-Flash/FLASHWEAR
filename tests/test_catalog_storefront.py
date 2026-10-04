"""Tests for the public catalogue pages.

These are the tests that answer the question a shopper would: does the page exist, does it show the
product, and does it refuse to show what is not published. They also assert the metadata, because a
catalogue page nobody can find on a search engine is a catalogue page that does not exist.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------------------


class TestCatalogueUrls:
    def test_urls_are_slug_addressed(self):
        assert reverse("catalog:product-list") == "/products/"
        assert reverse("catalog:product-detail", kwargs={"slug": "x"}) == "/products/x/"
        assert reverse("catalog:category-list") == "/categories/"
        assert reverse("catalog:category-detail", kwargs={"slug": "men"}) == "/categories/men/"
        assert reverse("catalog:collection-list") == "/collections/"
        assert reverse("catalog:collection-detail", kwargs={"slug": "core"}) == "/collections/core/"
        assert reverse("catalog:brand-detail", kwargs={"slug": "flashwear"}) == "/brands/flashwear/"

    def test_namespace_is_catalog(self):
        from apps.catalog import urls

        assert urls.app_name == "catalog"

    def test_every_trailing_slash_is_consistent(self):
        """One shape for every URL: ``APPEND_SLASH`` and the CDN both assume it."""
        from apps.catalog import urls

        assert all(str(entry.pattern).endswith("/") for entry in urls.urlpatterns)


# --------------------------------------------------------------------------------------
# Product list
# --------------------------------------------------------------------------------------


class TestProductList:
    def test_lists_published_products(self, client, product, draft_product):
        response = client.get("/products/")
        assert response.status_code == 200
        assert product.name in response.content.decode()
        assert draft_product.name not in response.content.decode()

    def test_reports_a_product_count(self, client, product):
        assert "1 product" in client.get("/products/").content.decode()

    def test_pluralises_the_count(self, client, product, make_product):
        make_product(name="Second", slug="second")
        assert "2 products" in client.get("/products/").content.decode()

    def test_sort_control_marks_the_active_choice(self, client, product):
        html = client.get("/products/?sort=price_asc").content.decode()
        assert 'aria-current="true"' in html

    def test_unknown_sort_falls_back_without_erroring(self, client, product):
        assert client.get("/products/?sort=price;drop").status_code == 200

    def test_price_sort_orders_by_price(self, client, make_product, colour, size, make_variant):
        dear = make_product(name="Dear", slug="dear", published_at=_days_ago(2))
        make_variant(dear, color=colour, size=size, price=Decimal("90.00"), sku="DEAR")
        cheap = make_product(name="Cheap", slug="cheap", published_at=_days_ago(1))
        make_variant(cheap, color=colour, size=size, price=Decimal("10.00"), sku="CHEAP")
        html = client.get("/products/?sort=price_asc").content.decode()
        assert html.index("Cheap") < html.index("Dear")

    def test_empty_catalogue_renders_an_honest_empty_state(self, client, db):
        response = client.get("/products/")
        assert response.status_code == 200
        assert "Nothing here yet" in response.content.decode()

    def test_department_chips_link_to_departments(self, client, department):
        """Chips are the two levels a shopper navigates by, so roots, not leaves."""
        html = client.get("/products/").content.decode()
        assert f'href="{department.get_absolute_url()}"' in html

    def test_only_get_is_allowed(self, client):
        assert client.post("/products/").status_code == 405

    def test_pages_are_not_cached(self, client):
        """Prices and sale flags change without a deploy."""
        response = client.get("/products/")
        assert "no-store" in response.headers.get("Cache-Control", "")


class TestProductPagination:
    @pytest.fixture
    def many_products(self, make_product, category):
        for index in range(settings_page_size() + 2):
            make_product(name=f"Paged {index:02d}", slug=f"paged-{index:02d}")

    def test_pages_do_not_overlap(self, client, many_products):
        """A product must not appear on two pages, and none may be skipped."""
        first = client.get("/products/").content.decode()
        second = client.get("/products/?page=2").content.decode()
        assert set(_product_names(first)).isdisjoint(_product_names(second))
        assert len(_product_names(first)) == settings_page_size()
        assert len(_product_names(second)) == 2

    def test_out_of_range_page_is_the_last_page(self, client, many_products):
        response = client.get("/products/?page=999")
        assert response.status_code == 200

    def test_nonsense_page_is_page_one(self, client, many_products):
        assert client.get("/products/?page=banana").status_code == 200

    def test_pagination_links_keep_the_sort(self, client, many_products):
        html = client.get("/products/?sort=price_asc").content.decode()
        assert "sort=price_asc" in html

    def test_previous_is_disabled_on_page_one(self, client, many_products):
        html = client.get("/products/").content.decode()
        assert "cursor-not-allowed" in html


# --------------------------------------------------------------------------------------
# Product detail
# --------------------------------------------------------------------------------------


class TestProductDetail:
    def test_a_live_product_is_reachable(self, client, full_product):
        response = client.get(full_product.get_absolute_url())
        assert response.status_code == 200
        assert full_product.name in response.content.decode()

    def test_a_draft_product_is_not_found(self, client, draft_product):
        """Not hidden by a template check: the queryset cannot return it."""
        assert client.get(draft_product.get_absolute_url()).status_code == 404

    def test_an_archived_product_is_not_found(self, client, archived_product):
        assert client.get(archived_product.get_absolute_url()).status_code == 404

    def test_a_scheduled_product_is_not_found_yet(self, client, scheduled_product):
        assert client.get(scheduled_product.get_absolute_url()).status_code == 404

    def test_unknown_slug_is_not_found(self, client, db):
        assert client.get("/products/does-not-exist/").status_code == 404

    def test_the_price_is_shown(self, client, full_product):
        assert "49.00" in client.get(full_product.get_absolute_url()).content.decode()

    def test_colour_links_preserve_the_other_selection(
        self, client, full_product, other_colour, other_size
    ):
        html = client.get(f"{full_product.get_absolute_url()}?color=black").content.decode()
        assert f"size={other_size.code}" in html
        assert f"color={other_colour.slug}" in html

    def test_selecting_a_colour_changes_the_selected_sku(
        self, client, make_product, colour, other_colour, size, make_variant
    ):
        """Prices live on variants, so the selection has to reach the query."""
        product = make_product(name="Two Priced Colours", slug="two-priced-colours")
        make_variant(product, color=colour, size=size, price=Decimal("49.00"), sku="BLACK")
        make_variant(product, color=other_colour, size=size, price=Decimal("79.00"), sku="SAND")

        sand = client.get(f"{product.get_absolute_url()}?color=sand").content.decode()
        black = client.get(f"{product.get_absolute_url()}?color=black").content.decode()
        assert "SAND" in sand
        assert "BLACK" not in sand
        assert "BLACK" in black

    def test_an_impossible_combination_falls_back(self, client, full_product):
        response = client.get(f"{full_product.get_absolute_url()}?color=chartreuse&size=XXXL")
        assert response.status_code == 200

    def test_a_stale_bookmark_still_renders(self, client, full_product):
        assert client.get(f"{full_product.get_absolute_url()}?color=chartreuse").status_code == 200

    def test_the_add_control_waits_for_a_variant_choice(self, client, make_product):
        """With nothing buyable to add, the control says so instead of posting a SKU."""
        product = make_product(name="No Sizes Yet", slug="no-sizes-yet")
        html = client.get(product.get_absolute_url()).content.decode()
        assert "Choose a colour and size" in html
        assert "disabled" in html

    def test_a_resolvable_variant_offers_a_real_add_to_bag_form(self, client, product):
        """A product whose variant can be resolved posts straight to the cart."""
        html = client.get(product.get_absolute_url()).content.decode()
        assert "Add to bag" in html
        assert 'action="/shop/cart/add/"' in html
        assert "Availability will be confirmed before order placement." in html

    def test_no_stock_claim_is_made(self, client, full_product):
        """Availability is not knowable until Phase 4, so the page must not imply it."""
        html = client.get(full_product.get_absolute_url()).content.decode().lower()
        assert "in stock" not in html
        assert "only " not in html.split("sku")[0]

    def test_sku_is_visible_for_the_selected_variant(self, client, product):
        html = client.get(product.get_absolute_url()).content.decode()
        assert "FW-TEE-BLK-M" in html

    def test_breadcrumb_links_the_category(self, client, full_product, category):
        html = client.get(full_product.get_absolute_url()).content.decode()
        assert f'href="{category.get_absolute_url()}"' in html

    def test_related_products_appear(self, client, product, make_product):
        sibling = make_product(name="Sibling", slug="sibling", brand=product.brand)
        html = client.get(product.get_absolute_url()).content.decode()
        assert sibling.name in html

    def test_related_products_stay_out_of_the_way_when_alone(self, client, product):
        html = client.get(product.get_absolute_url()).content.decode()
        assert "You may also like" not in html

    def test_a_product_without_variants_still_renders(self, client, make_product):
        product = make_product(name="No Sizes Yet", slug="no-sizes-yet")
        response = client.get(product.get_absolute_url())
        assert response.status_code == 200

    def test_missing_image_alt_is_never_rendered_empty(self, client, product):
        from apps.catalog.models import ProductImage

        ProductImage.objects.create(
            product=product, image="catalog/tests/alt.png", alt_text="Black tee, front", position=0
        )
        html = client.get(product.get_absolute_url()).content.decode()
        assert "Black tee, front" in html


# --------------------------------------------------------------------------------------
# Metadata and structured data
# --------------------------------------------------------------------------------------


class TestProductMetadata:
    def test_title_falls_back_to_the_product_name(self, client, product):
        assert "Heavyweight Tee" in client.get(product.get_absolute_url()).content.decode()

    def test_explicit_seo_title_wins(self, client, product):
        product.seo_title = "The Weighty Tee | FLASHWEAR"
        product.save()
        assert (
            "The Weighty Tee | FLASHWEAR" in client.get(product.get_absolute_url()).content.decode()
        )

    def test_description_falls_back_to_the_short_description(self, client, product):
        html = client.get(product.get_absolute_url()).content.decode()
        assert product.short_description in html

    def test_description_falls_back_to_the_description(self, client, product):
        """With no hand-written copy the page still says what the garment is."""
        product.short_description = ""
        product.seo_description = ""
        product.save()
        html = client.get(product.get_absolute_url()).content.decode()
        assert "garment-dyed" in html

    def test_canonical_url_is_absolute(self, client, product):
        html = client.get(product.get_absolute_url()).content.decode()
        assert f"http://testserver{product.get_absolute_url()}" in html

    def test_canonical_is_the_bare_path_not_the_query(self, client, full_product):
        """?color=black is the same product and must not compete with the canonical URL."""
        html = client.get(f"{full_product.get_absolute_url()}?color=black").content.decode()
        assert f'rel="canonical" href="http://testserver{full_product.get_absolute_url()}"' in html

    def test_open_graph_type_is_product(self, client, product):
        assert (
            'property="og:type" content="product"'
            in client.get(product.get_absolute_url()).content.decode()
        )

    def test_json_ld_product_is_emitted(self, client, full_product):

        payload = _json_ld(client, full_product.get_absolute_url(), "ld-product")
        assert payload["@type"] == "Product"
        assert payload["name"] == full_product.name
        assert payload["brand"]["name"] == full_product.brand.name
        assert payload["offers"]["lowPrice"] == "49.00"

    def test_json_ld_offers_use_the_aggregate_range(
        self, client, make_product, colour, size, make_variant
    ):

        product = make_product(name="Ranged", slug="ranged")
        make_variant(product, color=colour, size=size, price=Decimal("10.00"), sku="LOW")
        make_variant(product, color=None, size=None, price=Decimal("30.00"), sku="HIGH")
        payload = _json_ld(client, product.get_absolute_url(), "ld-product")
        assert payload["offers"]["lowPrice"] == "10.00"
        assert payload["offers"]["highPrice"] == "30.00"
        assert payload["offers"]["offerCount"] == 2

    def test_json_ld_makes_no_availability_claim(self, client, product):
        payload = _json_ld(client, product.get_absolute_url(), "ld-product")
        assert "availability" not in payload["offers"]

    def test_json_ld_omits_offers_without_a_price(self, client, make_product):
        product = make_product(name="Unpriced", slug="unpriced")
        payload = _json_ld(client, product.get_absolute_url(), "ld-product")
        assert "offers" not in payload

    def test_breadcrumb_json_ld_is_emitted(self, client, full_product):

        payload = _json_ld(client, full_product.get_absolute_url(), "ld-breadcrumb")
        assert payload["@type"] == "BreadcrumbList"
        assert [item["name"] for item in payload["itemListElement"]] == [
            "Home",
            full_product.category.name,
            full_product.name,
        ]

    def test_description_is_truncated_to_a_search_length(self, client, product):
        from apps.catalog.seo import _truncate

        assert len(_truncate("word " * 100, 160)) <= 161


# --------------------------------------------------------------------------------------
# Categories, collections, brands
# --------------------------------------------------------------------------------------


class TestCategoryPages:
    def test_index_lists_departments_and_children(self, client, category):
        html = client.get("/categories/").content.decode()
        assert "Men" in html
        assert "T-Shirts" in html

    def test_department_lists_its_subtree(self, client, product, other_category, department):
        html = client.get(department.get_absolute_url()).content.decode()
        assert product.name in html

    def test_empty_leaf_shows_the_empty_state(self, client, other_category):
        assert "Nothing here yet" in client.get(other_category.get_absolute_url()).content.decode()

    def test_inactive_category_is_not_reachable(self, client, inactive_category):
        assert client.get(inactive_category.get_absolute_url()).status_code == 404

    def test_unknown_category_is_not_found(self, client, db):
        assert client.get("/categories/nope/").status_code == 404

    def test_breadcrumb_reflects_the_trail(self, client, category, department):
        html = client.get(category.get_absolute_url()).content.decode()
        assert department.name in html

    def test_subcategory_chips_are_shown(self, client, department, other_category):
        html = client.get(department.get_absolute_url()).content.decode()
        assert f'href="{other_category.get_absolute_url()}"' in html


class TestCollectionPages:
    def test_index_lists_live_collections(self, client, collection):
        assert collection.name in client.get("/collections/").content.decode()

    def test_collection_page_lists_its_products(self, client, product, collection):
        collection.products.add(product)
        assert product.name in client.get(collection.get_absolute_url()).content.decode()

    def test_inactive_collection_is_not_reachable(self, client, collection):
        collection.is_active = False
        collection.save()
        assert client.get(collection.get_absolute_url()).status_code == 404

    def test_out_of_window_collection_is_not_reachable(self, client, db):
        from django.utils import timezone

        from apps.catalog.models import Collection

        finished = Collection.objects.create(
            name="Finished", slug="finished", ends_at=timezone.now() - timezone.timedelta(days=1)
        )
        assert client.get(finished.get_absolute_url()).status_code == 404

    def test_future_collection_is_not_reachable(self, client, db):
        from django.utils import timezone

        from apps.catalog.models import Collection

        upcoming = Collection.objects.create(
            name="Upcoming", slug="upcoming", starts_at=timezone.now() + timezone.timedelta(days=1)
        )
        assert client.get(upcoming.get_absolute_url()).status_code == 404

    def test_index_is_empty_without_collections(self, client, db):
        assert "No collections are running" in client.get("/collections/").content.decode()


class TestBrandPages:
    def test_brand_page_lists_its_products(self, client, product):
        assert product.name in client.get("/brands/flashwear/").content.decode()

    def test_inactive_brand_is_not_reachable(self, client, brand):
        brand.is_active = False
        brand.save()
        assert client.get(brand.get_absolute_url()).status_code == 404

    def test_unknown_brand_is_not_found(self, client, db):
        assert client.get("/brands/nope/").status_code == 404


# --------------------------------------------------------------------------------------
# Integration with the rest of the site
# --------------------------------------------------------------------------------------


class TestHomepageIntegration:
    def test_featured_products_appear_on_the_homepage(self, client, full_product):
        assert full_product.name in client.get("/").content.decode()

    def test_new_arrivals_section_is_rendered(self, client, make_product):
        """A single product is not a "new arrivals" shelf, so two are needed."""
        first = make_product(name="New One", slug="new-one")
        second = make_product(name="New Two", slug="new-two")
        first.is_new = True
        second.is_new = True
        first.save()
        second.save()
        html = client.get("/").content.decode()
        assert "New arrivals" in html
        assert first.name in html
        assert second.name in html

    def test_department_links_are_present(self, client, department):
        html = client.get("/").content.decode()
        assert f'href="{department.get_absolute_url()}"' in html

    def test_homepage_survives_an_empty_catalogue(self, client, db):
        response = client.get("/")
        assert response.status_code == 200
        assert "Nothing is featured yet" in response.content.decode()

    def test_navbar_links_to_the_catalogue(self, client, db):
        html = client.get("/").content.decode()
        assert 'href="/products/"' in html
        assert 'href="/collections/"' in html
        assert 'href="/categories/"' in html

    def test_footer_links_to_the_catalogue(self, client, db):
        html = client.get("/").content.decode()
        assert html.count('href="/products/"') >= 2

    def test_outfits_and_drops_stay_visibly_inert(self, client, db):
        """Later phases own those links; pretending they work would be worse than not linking."""
        html = client.get("/").content.decode()
        assert "coming soon" in html.lower()


class TestMaintenanceModeStillApplies:
    def test_maintenance_page_covers_the_whole_storefront(
        self, client, site_configuration, product
    ):
        """Closing the shop for a deployment must not leave the catalogue reachable."""
        site_configuration.maintenance_mode = True
        site_configuration.save()
        for path in (
            "/",
            "/products/",
            product.get_absolute_url(),
            "/collections/",
            "/categories/",
        ):
            assert client.get(path).status_code == 503, path

    def test_maintenance_mode_does_not_lock_out_customers(self, client, site_configuration, user):
        """An address book that 503s during a deploy would lose orders, not gain them."""
        site_configuration.maintenance_mode = True
        site_configuration.save()
        client.force_login(user)
        assert client.get("/account/").status_code == 200

    def test_staff_still_see_the_site_during_maintenance(
        self, client, site_configuration, admin_user
    ):
        """Whoever turned the switch on has to be able to see what they turned it off for."""
        site_configuration.maintenance_mode = True
        site_configuration.save()
        client.force_login(admin_user)
        assert client.get("/products/").status_code == 200


class TestCurrencyFormatting:
    def test_prices_are_rendered_with_the_currency_symbol(self, client, product):
        html = client.get("/products/").content.decode()
        assert settings_symbol() in html

    def test_money_filter_formats_two_decimals(self):
        from apps.catalog.templatetags.catalog_extras import money

        assert money(Decimal("49.00")) == f"{settings_symbol()}49.00"
        assert money(Decimal("1290.00")) == f"{settings_symbol()}1,290.00"

    def test_money_filter_handles_a_missing_price(self):
        from apps.catalog.templatetags.catalog_extras import money

        assert money(None) == ""


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _product_names(html: str) -> set[str]:
    """Product names as they appear inside product card headings."""
    import re

    return set(
        re.findall(
            r'<h3 class="text-sm font-semibold text-slate-900">\s*<a[^>]*>\s*([^<]+?)\s*</a>', html
        )
    )


def _json_ld(client, url: str, element_id: str) -> dict:
    """Extract a ``json_script`` payload from rendered HTML."""
    import json
    import re

    html = client.get(url).content.decode()
    match = re.search(
        rf'<script id="{re.escape(element_id)}" type="application/json">(.*?)</script>', html, re.S
    )
    assert match, f"{element_id} was not emitted"
    return json.loads(match.group(1))


def _days_ago(days: int):
    from django.utils import timezone

    return timezone.now() - timezone.timedelta(days=days)


def settings_page_size() -> int:
    from django.conf import settings

    return settings.CATALOG_PRODUCTS_PER_PAGE


def settings_symbol() -> str:
    from django.conf import settings

    return settings.CATALOG_CURRENCY_SYMBOL
