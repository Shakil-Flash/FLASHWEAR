"""Tests for the catalogue REST API.

The contract these tests protect:

* **public and read-only** -- no authentication, no writes, 405 on anything but GET;
* **same visibility as the storefront** -- a draft is a 404 here exactly as it is on the website;
* **no commercial secrets** -- ``cost_price`` and ``barcode`` are absent from every payload;
* **bounded** -- ``page_size`` is clamped, so the catalogue cannot be dumped in one request.
"""

from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------------------


class TestProductListEndpoint:
    def test_lists_published_products(self, api_client, product):
        response = api_client.get("/api/v1/products/")
        assert response.status_code == 200
        assert [row["slug"] for row in response.data["results"]] == [product.slug]

    def test_drafts_are_invisible(self, api_client, draft_product):
        assert api_client.get("/api/v1/products/").data["count"] == 0

    def test_archived_products_are_invisible(self, api_client, archived_product):
        assert api_client.get("/api/v1/products/").data["count"] == 0

    def test_scheduled_products_are_invisible_until_dated(self, api_client, scheduled_product):
        assert api_client.get("/api/v1/products/").data["count"] == 0

    def test_requires_no_authentication(self, api_client):
        api_client.credentials()
        assert api_client.get("/api/v1/products/").status_code == 200

    def test_rejects_post(self, api_client):
        assert api_client.post("/api/v1/products/", {}).status_code == 405

    def test_pagination_is_reported(self, api_client, product):
        payload = api_client.get("/api/v1/products/").data
        assert payload["count"] == 1
        assert payload["next"] is None
        assert payload["previous"] is None

    def test_page_size_is_honoured(self, api_client, make_product):
        for index in range(5):
            make_product(name=f"Item {index}", slug=f"item-{index}")
        payload = api_client.get("/api/v1/products/?page_size=2").data
        assert len(payload["results"]) == 2
        assert payload["count"] == 5

    def test_page_size_is_clamped(self, api_client, product, settings):
        from django.conf import settings as django_settings

        payload = api_client.get("/api/v1/products/?page_size=100000").data
        assert payload["count"] <= django_settings.CATALOG_API_MAX_PAGE_SIZE

    def test_filters_by_category_slug(self, api_client, product, other_category, make_product):
        elsewhere = make_product(name="Other", slug="other", category=other_category)
        payload = api_client.get(f"/api/v1/products/?category={product.category.slug}").data
        slugs = [row["slug"] for row in payload["results"]]
        assert slugs == [product.slug]
        assert elsewhere.slug not in slugs

    def test_filters_by_brand_slug(self, api_client, product, other_brand, make_product):
        make_product(name="Northline Tee", slug="northline-tee", brand=other_brand)
        payload = api_client.get("/api/v1/products/?brand=northline").data
        assert [row["slug"] for row in payload["results"]] == ["northline-tee"]

    def test_filters_by_collection_slug(self, api_client, product, collection):
        collection.products.add(product)
        payload = api_client.get("/api/v1/products/?collection=core-essentials").data
        assert payload["count"] == 1

    def test_unknown_filter_returns_an_empty_page(self, api_client, product):
        """A stale client bookmark should see an empty shelf, not a 400."""
        assert api_client.get("/api/v1/products/?category=gone").data["count"] == 0

    def test_sort_is_validated(self, api_client, product):
        assert api_client.get("/api/v1/products/?sort=nonsense").status_code == 200

    def test_price_sort_orders_the_page(self, api_client, make_product, colour, size, make_variant):
        dear = make_product(name="Dear", slug="dear")
        make_variant(dear, color=colour, size=size, price=Decimal("90.00"), sku="DEAR")
        cheap = make_product(name="Cheap", slug="cheap")
        make_variant(cheap, color=colour, size=size, price=Decimal("10.00"), sku="CHEAP")
        payload = api_client.get("/api/v1/products/?sort=price_asc").data
        assert [row["slug"] for row in payload["results"]] == ["cheap", "dear"]


class TestProductListPayload:
    def test_card_fields_are_present(self, api_client, full_product):
        row = api_client.get("/api/v1/products/").data["results"][0]
        for key in (
            "id",
            "name",
            "slug",
            "url",
            "short_description",
            "brand",
            "category",
            "price_min",
            "price_max",
            "image",
            "colors",
            "is_on_sale",
            "published_at",
        ):
            assert key in row

    def test_price_is_a_string_not_a_float(self, api_client, product):
        """JSON numbers are doubles; a price must not lose cents on the way to a client."""
        row = api_client.get("/api/v1/products/").data["results"][0]
        assert row["price_min"] == "49.00"
        assert isinstance(row["price_min"], str)

    def test_url_is_slug_addressed(self, api_client, product):
        row = api_client.get("/api/v1/products/").data["results"][0]
        assert row["url"] == product.get_absolute_url()

    def test_colours_are_included(self, api_client, full_product):
        row = api_client.get("/api/v1/products/").data["results"][0]
        assert [colour["slug"] for colour in row["colors"]] == ["black", "sand"]

    def test_cost_price_is_never_serialised(
        self, api_client, product, other_colour, other_size, make_variant
    ):
        """The margin is staff information and must not be reachable without a session."""
        make_variant(
            product,
            color=other_colour,
            size=other_size,
            price=Decimal("49.00"),
            sku="CHEAP",
            cost_price=Decimal("5.00"),
        )
        assert "cost_price" not in str(api_client.get("/api/v1/products/").data)
        detail = api_client.get(f"/api/v1/products/{product.slug}/").data
        assert "cost_price" not in str(detail)

    def test_barcode_is_never_serialised(self, api_client, product):
        assert "barcode" not in str(api_client.get("/api/v1/products/").data)

    def test_sale_flag_is_present(self, api_client, full_product):
        assert api_client.get("/api/v1/products/").data["results"][0]["is_on_sale"] is True


class TestProductDetailEndpoint:
    def test_returns_the_product_with_its_variants(self, api_client, full_product):
        payload = api_client.get(f"/api/v1/products/{full_product.slug}/").data
        assert payload["slug"] == full_product.slug
        assert len(payload["variants"]) == 4

    def test_includes_descriptive_fields(self, api_client, full_product):
        payload = api_client.get(f"/api/v1/products/{full_product.slug}/").data
        assert payload["description"]
        assert payload["fit"] == "Oversized"
        assert payload["materials"] == ["Organic cotton"]
        assert payload["tags"] == ["New in"]

    def test_draft_product_is_404(self, api_client, draft_product):
        assert api_client.get(f"/api/v1/products/{draft_product.slug}/").status_code == 404

    def test_archived_product_is_404(self, api_client, archived_product):
        assert api_client.get(f"/api/v1/products/{archived_product.slug}/").status_code == 404

    def test_unknown_slug_is_404(self, api_client, db):
        assert api_client.get("/api/v1/products/nope/").status_code == 404

    def test_detail_query_does_not_hide_behind_a_bare_404(self, api_client, full_product):
        """A bare "Not found." is DRF swallowing a ValueError, not a missing product."""
        response = api_client.get(f"/api/v1/products/{full_product.slug}/")
        assert response.status_code == 200
        assert response.data["slug"] == full_product.slug

    def test_variant_prices_are_strings(self, api_client, full_product):
        payload = api_client.get(f"/api/v1/products/{full_product.slug}/").data
        assert payload["variants"][0]["price"] == "49.00"

    def test_inactive_variants_are_not_exposed(self, api_client, full_product, make_variant):
        make_variant(full_product, price=Decimal("1.00"), sku="HIDDEN", is_active=False)
        payload = api_client.get(f"/api/v1/products/{full_product.slug}/").data
        assert "HIDDEN" not in {variant["sku"] for variant in payload["variants"]}

    def test_rejects_post(self, api_client, full_product):
        assert api_client.post(f"/api/v1/products/{full_product.slug}/", {}).status_code == 405

    def test_responses_are_not_cached(self, api_client, full_product):
        response = api_client.get(f"/api/v1/products/{full_product.slug}/")
        assert "no-store" in response.headers.get("Cache-Control", "")


# --------------------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------------------


class TestCategoryEndpoints:
    def test_index_lists_root_categories(self, api_client, department):
        payload = api_client.get("/api/v1/categories/").data
        assert [row["slug"] for row in payload["results"]] == ["men"]

    def test_index_includes_children(self, api_client, department, category):
        row = api_client.get("/api/v1/categories/").data["results"][0]
        assert [child["slug"] for child in row["children"]] == ["t-shirts"]

    def test_index_reports_a_subtree_count(self, api_client, department, product):
        row = api_client.get("/api/v1/categories/").data["results"][0]
        assert row["product_count"] == 1
        assert row["subcategory_count"] == 1

    def test_inactive_categories_are_hidden(self, api_client, inactive_category):
        payload = api_client.get("/api/v1/categories/").data
        assert all(row["slug"] != "archive" for row in payload["results"])

    def test_detail_returns_one_category(self, api_client, category, product):
        payload = api_client.get("/api/v1/categories/t-shirts/").data
        assert payload["slug"] == "t-shirts"
        assert payload["product_count"] == 1
        assert payload["parent"]["slug"] == "men"

    def test_detail_is_404_for_unknown(self, api_client, db):
        assert api_client.get("/api/v1/categories/nope/").status_code == 404

    def test_detail_is_404_for_inactive(self, api_client, inactive_category):
        assert api_client.get("/api/v1/categories/archive/").status_code == 404


class TestBrandEndpoints:
    def test_index_lists_brands_with_published_products(
        self, api_client, brand, other_brand, product
    ):
        """``other_brand`` has no products, so advertising it would be a dead link."""
        payload = api_client.get("/api/v1/brands/").data
        assert [row["slug"] for row in payload["results"]] == ["flashwear"]

    def test_index_reports_the_product_count(self, api_client, brand, product):
        assert api_client.get("/api/v1/brands/").data["results"][0]["product_count"] == 1

    def test_detail_returns_the_brand(self, api_client, brand):
        assert api_client.get("/api/v1/brands/flashwear/").data["name"] == "FLASHWEAR"

    def test_inactive_brand_is_404(self, api_client, brand):
        brand.is_active = False
        brand.save()
        assert api_client.get("/api/v1/brands/flashwear/").status_code == 404


class TestCollectionEndpoints:
    def test_index_lists_live_collections(self, api_client, collection):
        payload = api_client.get("/api/v1/collections/").data
        assert [row["slug"] for row in payload["results"]] == ["core-essentials"]

    def test_out_of_window_collection_is_hidden(self, api_client, db):
        from django.utils import timezone

        from apps.catalog.models import Collection

        Collection.objects.create(
            name="Finished", slug="finished", ends_at=timezone.now() - timezone.timedelta(days=1)
        )
        assert api_client.get("/api/v1/collections/").data["count"] == 0

    def test_live_flag_is_reported(self, api_client, collection):
        assert api_client.get("/api/v1/collections/").data["results"][0]["is_live"] is True

    def test_detail_returns_the_collection(self, api_client, collection):
        assert (
            api_client.get("/api/v1/collections/core-essentials/").data["slug"] == "core-essentials"
        )

    def test_out_of_window_detail_is_404(self, api_client, db):
        from django.utils import timezone

        from apps.catalog.models import Collection

        Collection.objects.create(
            name="Finished",
            slug="finished",
            ends_at=timezone.now() - timezone.timedelta(days=1),
        )
        assert api_client.get("/api/v1/collections/finished/").status_code == 404


# --------------------------------------------------------------------------------------
# Root and method hygiene
# --------------------------------------------------------------------------------------


class TestCatalogueRoot:
    def test_root_advertises_the_catalogue(self, api_client):
        endpoints = api_client.get("/api/v1/").data["endpoints"]
        for name in ("products", "categories", "collections", "brands"):
            assert name in endpoints

    def test_advertised_urls_resolve(self, api_client, product):
        from urllib.parse import urlparse

        from django.urls import resolve

        endpoints = api_client.get("/api/v1/").data["endpoints"]
        assert resolve(urlparse(endpoints["products"]).path).url_name == "product-list"
        assert resolve(urlparse(endpoints["categories"]).path).url_name == "category-list"

    def test_every_catalogue_endpoint_is_read_only(self, api_client, full_product):
        paths = [
            "/api/v1/products/",
            f"/api/v1/products/{full_product.slug}/",
            "/api/v1/categories/",
            "/api/v1/categories/t-shirts/",
            "/api/v1/collections/",
            "/api/v1/collections/core-essentials/",
            "/api/v1/brands/",
            "/api/v1/brands/flashwear/",
        ]
        for path in paths:
            assert api_client.post(path, {}).status_code == 405, path
            assert api_client.put(path, {}).status_code == 405, path
            assert api_client.delete(path).status_code == 405, path

    def test_catalogue_endpoints_are_never_cached(self, api_client, product):
        for path in ("/api/v1/products/", "/api/v1/categories/", "/api/v1/brands/"):
            response = api_client.get(path)
            assert "no-store" in response.headers.get("Cache-Control", ""), path
