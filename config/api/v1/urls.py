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
from apps.closet.api import (
    ClosetItemDetailView,
    ClosetItemListView,
    OutfitDetailView,
    OutfitListView,
)
from apps.core.api import health
from apps.drops.api import (
    DropDetailView,
    DropInterestView,
    DropProductsView,
    DropRootView,
)
from apps.engagement.api import ProductReviewsView, PromotionValidateView, ReviewDetailView
from apps.loop.api import (
    LoopCreditListView,
    LoopItemCancelView,
    LoopItemDetailView,
    LoopItemListCreateView,
    LoopItemSubmitView,
    LoopRootView,
    RecycleListCreateView,
    ResaleListingDetailView,
    ResaleListingListView,
    TradeInListCreateView,
)
from apps.orders.api import OrderDetailView, OrderListView
from apps.recommendations.api import (
    ClosetComplementView,
    FeedbackView,
    ForYouView,
    NewForYouView,
    OutfitCompletionView,
    RecommendationRootView,
    SimilarProductsView,
)
from apps.styling.api import (
    FlashDNACheckView,
    FlashDNAView,
    StylistRecommendView,
    StylistSaveOutfitView,
    StylistStyleProductView,
)
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
    # Phase 8: FLASH Closet and outfits (customer-scoped).
    path("closet/", ClosetItemListView.as_view(), name="closet-item-list"),
    path("closet/<int:pk>/", ClosetItemDetailView.as_view(), name="closet-item-detail"),
    path("outfits/", OutfitListView.as_view(), name="outfit-list"),
    path("outfits/<int:pk>/", OutfitDetailView.as_view(), name="outfit-detail"),
    # Phase 9: FLASH DNA and AI Fashion Stylist (session-authenticated).
    path("flash-dna/", FlashDNAView.as_view(), name="flash-dna"),
    path("flash-dna/check/", FlashDNACheckView.as_view(), name="flash-dna-check"),
    path("stylist/recommend/", StylistRecommendView.as_view(), name="stylist-recommend"),
    path("stylist/style-product/", StylistStyleProductView.as_view(), name="stylist-style-product"),
    path("stylist/save-outfit/", StylistSaveOutfitView.as_view(), name="stylist-save-outfit"),
    # Phase 10: personalized discovery and recommendations (session-authenticated).
    path("recommendations/", RecommendationRootView.as_view(), name="recommendations-root"),
    path("recommendations/for-you/", ForYouView.as_view(), name="for-you"),
    path("recommendations/closet/", ClosetComplementView.as_view(), name="closet-complement"),
    path(
        "recommendations/outfit/<int:outfit_id>/",
        OutfitCompletionView.as_view(),
        name="outfit-completion",
    ),
    path("recommendations/similar/", SimilarProductsView.as_view(), name="similar-products"),
    path("recommendations/new-for-you/", NewForYouView.as_view(), name="new-for-you"),
    path("recommendations/feedback/", FeedbackView.as_view(), name="feedback"),
    # Phase 11: FLASH Drops and Smart Collections (session-authenticated).
    path("drops/", DropRootView.as_view(), name="drop-root"),
    path("drops/<slug:slug>/", DropDetailView.as_view(), name="drop-detail"),
    path("drops/<slug:slug>/products/", DropProductsView.as_view(), name="drop-products"),
    path("drops/<int:id>/interest/", DropInterestView.as_view(), name="drop-interest"),
    # Phase 13: FLASH Loop (resale shelf public; items/trade-ins/recycling
    # session-authenticated and customer-scoped).
    path("loop/", LoopRootView.as_view(), name="loop-root"),
    path("loop/resale/", ResaleListingListView.as_view(), name="loop-resale-list"),
    path(
        "loop/resale/<slug:slug>/",
        ResaleListingDetailView.as_view(),
        name="loop-resale-detail",
    ),
    path("loop/items/", LoopItemListCreateView.as_view(), name="loop-item-list"),
    path("loop/items/<int:pk>/", LoopItemDetailView.as_view(), name="loop-item-detail"),
    path(
        "loop/items/<int:pk>/submit/",
        LoopItemSubmitView.as_view(),
        name="loop-item-submit",
    ),
    path(
        "loop/items/<int:pk>/cancel/",
        LoopItemCancelView.as_view(),
        name="loop-item-cancel",
    ),
    path("loop/trade-ins/", TradeInListCreateView.as_view(), name="loop-trade-in-list"),
    path("loop/recycling/", RecycleListCreateView.as_view(), name="loop-recycle-list"),
    path("loop/credits/", LoopCreditListView.as_view(), name="loop-credit-list"),
]
