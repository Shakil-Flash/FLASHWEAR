"""Catalogue URLs.

Mounted at ``/`` by :mod:`config.urls`, so every pattern here is a top-level shopper URL and the
namespace is ``catalog``.

Slugs, not primary keys, in every public URL: a URL is a promise that survives a re-import, and
``/products/42/`` is not. Trailing slashes are consistent across the project because
``APPEND_SLASH`` and the CDN both assume one shape.
"""

from django.urls import path

from apps.catalog import views
from apps.engagement.views import review_create

app_name = "catalog"

urlpatterns = [
    # Specific names before the catch-all-ish patterns; each slug pattern is anchored to its own
    # prefix, so order here is only for readability.
    path("products/", views.product_list, name="product-list"),
    path("search/", views.product_search, name="product-search"),
    # Phase 7: the review form lives on the product page and posts here (view in engagement,
    # URL in the namespace the page itself belongs to -- same split as the account area).
    path("products/<slug:slug>/review/", review_create, name="review-create"),
    path("products/<slug:slug>/", views.product_detail, name="product-detail"),
    path("categories/", views.category_list, name="category-list"),
    path("categories/<slug:slug>/", views.category_detail, name="category-detail"),
    path("collections/", views.collection_list, name="collection-list"),
    path("collections/<slug:slug>/", views.collection_detail, name="collection-detail"),
    path("brands/<slug:slug>/", views.brand_detail, name="brand-detail"),
]
