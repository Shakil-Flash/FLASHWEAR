"""API v1 (``/api/v1/``).

The URL namespace is the versioning strategy: new capabilities get a new namespace
(``v2``) rather than breaking existing clients.
"""

from django.urls import path

from apps.accounts.api import (
    AccountMeView,
    AddressDetailView,
    AddressListCreateView,
    ChangePasswordView,
)
from apps.accounts.api import RegisterView as AccountRegisterView
from apps.catalog.api import (
    BrandDetailView,
    BrandListView,
    CategoryDetailView,
    CategoryListView,
    CollectionDetailView,
    CollectionListView,
    ProductDetailView,
    ProductListView,
    ProductSearchSuggestionsView,
    ProductSearchView,
)
from apps.core.api import health
from apps.engagement.api import ProductReviewsView, PromotionValidateView, ReviewDetailView
from apps.orders.api import OrderDetailView, OrderListView
from config.api.v1.views import ApiRootView

app_name = "v1"

urlpatterns = [
    path("", ApiRootView.as_view(), name="root"),
    path("health/", health, name="health"),
    # Catalogue: public, read-only, paginated.
    path("products/", ProductListView.as_view(), name="product-list"),
    path("products/search/", ProductSearchView.as_view(), name="product-search"),
    path(
        "products/suggestions/", ProductSearchSuggestionsView.as_view(), name="product-suggestions"
    ),
    # Engagement (Phase 7): reviews live under their product; writes are session-authenticated.
    path(
        "products/<slug:slug>/reviews/",
        ProductReviewsView.as_view(),
        name="product-reviews",
    ),
    path("reviews/<int:pk>/", ReviewDetailView.as_view(), name="review-detail"),
    path(
        "checkout/promotion/",
        PromotionValidateView.as_view(),
        name="checkout-promotion",
    ),
    path("products/<slug:slug>/", ProductDetailView.as_view(), name="product-detail"),
    path("categories/", CategoryListView.as_view(), name="category-list"),
    path("categories/<slug:slug>/", CategoryDetailView.as_view(), name="category-detail"),
    path("collections/", CollectionListView.as_view(), name="collection-list"),
    path("collections/<slug:slug>/", CollectionDetailView.as_view(), name="collection-detail"),
    path("brands/", BrandListView.as_view(), name="brand-list"),
    path("brands/<slug:slug>/", BrandDetailView.as_view(), name="brand-detail"),
    # Accounts: session-authenticated and customer-scoped (Phase 2).
    path("accounts/", AccountRegisterView.as_view(), name="account-register"),
    path("accounts/me/", AccountMeView.as_view(), name="account-me"),
    path("accounts/password/", ChangePasswordView.as_view(), name="account-password"),
    path("addresses/", AddressListCreateView.as_view(), name="address-list"),
    path("addresses/<int:pk>/", AddressDetailView.as_view(), name="address-detail"),
    # Orders: session-authenticated and customer-scoped (Phase 6).
    path("orders/", OrderListView.as_view(), name="order-list"),
    path("orders/<str:number>/", OrderDetailView.as_view(), name="order-detail"),
]
