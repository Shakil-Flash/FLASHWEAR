"""Phase 20: the search-engine surface.

Canonicals and index directives (page two stays out of the index, ``/search/`` is canonical
to itself), the social cards, the crawler files, and the structured data -- every payload
asserted here must be built from facts the shop actually holds, because structured data
that lies is worse than none.
"""

from __future__ import annotations

import json
import re

import pytest

from apps.catalog.seo import listing_metadata, taxonomy_metadata

pytestmark = pytest.mark.django_db


def _canonical(response) -> str:
    match = re.search(r'<link rel="canonical" href="([^"]+)"', response.content.decode())
    assert match, "no canonical tag was rendered"
    return match.group(1)


def _json_script(response, element_id: str) -> dict:
    html = response.content.decode()
    match = re.search(
        rf'<script id="{re.escape(element_id)}" type="application/json">(.*?)</script>', html, re.S
    )
    assert match, f"{element_id} was not emitted"
    return json.loads(match.group(1))


class TestCanonicalsAndIndexing:
    def test_the_search_page_is_canonical_to_itself(self, client):
        from django.urls import reverse

        response = client.get(f"{reverse('catalog:product-search')}?q=hoodie")
        assert _canonical(response).endswith(reverse("catalog:product-search"))

    def test_a_query_on_the_plain_listing_stays_on_the_plain_listing(self, client):
        from django.urls import reverse

        response = client.get(f"{reverse('catalog:product-list')}?q=shirt")
        assert _canonical(response).endswith(reverse("catalog:product-list"))

    def test_sort_permutations_canonicalise_away(self, client):
        from django.urls import reverse

        response = client.get(f"{reverse('catalog:product-list')}?sort=price_asc")
        assert "price_asc" not in _canonical(response)

    def test_page_one_stays_indexable_and_page_two_does_not(self, client, product):
        page_one = client.get("/products/?page=1")
        assert 'content="index, follow"' in page_one.content.decode()

        # The directive is a function of the page number, so it is asserted where the
        # number lives rather than by manufacturing twenty-five products to paginate.
        assert listing_metadata("T", "D", page=1)["seo_robots"] == "index, follow"
        assert listing_metadata("T", "D", page=2)["seo_robots"] == "noindex, follow"

    def test_a_taxonomy_page_beyond_one_is_noindex(self, category):
        assert taxonomy_metadata(category, page=1)["seo_robots"] == "index, follow"
        assert taxonomy_metadata(category, page=2)["seo_robots"] == "noindex, follow"


class TestSocialCards:
    def test_a_page_without_an_image_declares_a_summary_card(self, client, product):
        html = client.get(product.get_absolute_url()).content.decode()
        assert 'name="twitter:card" content="summary"' in html

    def test_an_image_upgrades_the_card_and_is_carried_through(self, client, product):
        from apps.catalog.models import ProductImage

        ProductImage.objects.create(
            product=product, image="catalog/tests/hero.png", alt_text="Front", position=0
        )
        html = client.get(product.get_absolute_url()).content.decode()
        assert 'name="twitter:card" content="summary_large_image"' in html
        assert 'name="twitter:image"' in html
        assert 'property="og:image"' in html


class TestCrawlerFiles:
    def test_robots_txt_points_at_the_sitemap_and_closes_private_paths(self, client):
        response = client.get("/robots.txt")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/plain")
        body = response.content.decode()
        assert "Disallow: /operations/" in body
        assert "Disallow: /account/" in body
        assert "Disallow: /admin/" in body
        assert "Sitemap: http://testserver/sitemap.xml" in body

    def test_the_sitemap_lists_the_storefront_and_nothing_private(self, client, product):
        from django.urls import reverse

        body = client.get(reverse("core:sitemap")).content.decode()
        assert product.get_absolute_url() in body
        assert "/account/" not in body
        assert "/operations/" not in body


class TestSiteStructuredData:
    def test_the_home_page_describes_the_site_from_the_config_row(self, client, site_configuration):
        from django.urls import reverse

        response = client.get(reverse("core:home"))
        organization = _json_script(response, "ld-organization")
        website = _json_script(response, "ld-website")

        assert organization["@type"] == "Organization"
        assert organization["name"] == site_configuration.site_name
        assert website["@type"] == "WebSite"
        target = website["potentialAction"]["target"]["urlTemplate"]
        assert reverse("catalog:product-search") in target
        assert target.endswith("?q={search_term_string}")


class TestBreadcrumbs:
    def test_a_category_page_emits_a_breadcrumb_list(self, client, category):
        response = client.get(category.get_absolute_url())
        payload = _json_script(response, "ld-breadcrumb")
        assert payload["@type"] == "BreadcrumbList"
        names = [element["name"] for element in payload["itemListElement"]]
        assert names[-1] == category.name

    def test_the_category_index_emits_one_too(self, client):
        from django.urls import reverse

        payload = _json_script(client.get(reverse("catalog:category-list")), "ld-breadcrumb")
        assert payload["@type"] == "BreadcrumbList"


class TestResaleStructuredData:
    def test_the_resale_payload_is_truthful(self, client, user, product, admin_user):
        from tests.test_loop_phase13 import make_resale_draft, walk_resale_to_listed

        item = make_resale_draft(user, product.variants.first())
        listing = walk_resale_to_listed(item, admin_user)

        html = client.get(listing.get_absolute_url()).content.decode()
        match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        assert match, "no JSON-LD block on the resale page"
        payload = json.loads(match.group(1))

        assert payload["@context"] == "https://schema.org"  # no trailing slash
        assert "availability" not in payload["offers"]  # nothing is claimed about stock
        assert payload["url"].startswith("http://testserver/")
        assert payload["offers"]["price"]  # the real asking price
