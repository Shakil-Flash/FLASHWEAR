"""Sitemap entries for the storefront (Phase 20).

Only URLs a shopper can reach without logging in: the home page, published products and
the live taxonomy. Private surfaces (account, cart, checkout, operations, the API) are
absent rather than listed-and-disallowed -- a URL that should never be crawled has no
business in the index at all.

``lastmod`` is the row's own ``updated_at``, so the sitemap ages with the content instead
of lying about freshness.
"""

from __future__ import annotations

from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from apps.catalog.models import Brand, Category, Collection, Product

__all__ = [
    "BrandSitemap",
    "CategorySitemap",
    "CollectionSitemap",
    "HomeSitemap",
    "ProductSitemap",
    "sitemaps",
]


class HomeSitemap(Sitemap):
    priority = 1.0
    changefreq = "daily"

    def items(self):
        return ["core:home"]

    def location(self, item):
        return reverse(item)


class ProductSitemap(Sitemap):
    priority = 0.7
    changefreq = "daily"

    def items(self):
        return Product.objects.published()

    def lastmod(self, obj):
        return obj.updated_at


class CategorySitemap(Sitemap):
    priority = 0.6
    changefreq = "weekly"

    def items(self):
        return Category.objects.filter(is_active=True)

    def lastmod(self, obj):
        return obj.updated_at


class CollectionSitemap(Sitemap):
    priority = 0.6
    changefreq = "weekly"

    def items(self):
        # Outside its date window a collection answers 404, so only current ones belong
        # here; the window logic is a Python property, and the table is tiny.
        return [c for c in Collection.objects.filter(is_active=True) if c.is_current]

    def lastmod(self, obj):
        return obj.updated_at


class BrandSitemap(Sitemap):
    priority = 0.5
    changefreq = "weekly"

    def items(self):
        return Brand.objects.filter(is_active=True)

    def lastmod(self, obj):
        return obj.updated_at


sitemaps = {
    "home": HomeSitemap,
    "products": ProductSitemap,
    "categories": CategorySitemap,
    "collections": CollectionSitemap,
    "brands": BrandSitemap,
}
