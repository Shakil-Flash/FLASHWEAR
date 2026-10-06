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
from apps.backoffice.api import (
    BackOfficeAlertsView,
    BackOfficeAuditView,
    BackOfficeDashboardView,
    BackOfficeOrdersView,
    BackOfficeRootView,
)
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
    ProductSimilarView,
    ProductVisualSearchView,
    VisualProductClickView,
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
from apps.notifications.api import (
    NotificationListView,
    NotificationPreferencesView,
    NotificationReadAllView,
    NotificationReadView,
)
from apps.orders.api import OrderDetailView, OrderListView
from apps.quests.api import (
    QuestDetailAPIView,
    QuestListAPIView,
    QuestStartAPIView,
    RewardsAPIView,
)
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
    StudioCompleteLookView,
    StudioGenerateView,
    StudioGoalView,
    StudioSaveView,
    StylistRecommendView,
    StylistSaveOutfitView,
    StylistStyleProductView,
)
from apps.support.api import (
    StaffTicketAssignView,
    StaffTicketDetailView,
    StaffTicketEscalateView,
    StaffTicketLinkView,
    StaffTicketListView,
    StaffTicketNoteView,
    StaffTicketPriorityView,
    StaffTicketReplyView,
    StaffTicketStatusView,
    SupportRootView,
    SupportTicketCloseView,
    SupportTicketDetailView,
    SupportTicketListCreateView,
    SupportTicketMessagesView,
    SupportTicketReopenView,
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
        "products/visual-search/",
        ProductVisualSearchView.as_view(),
        name="product-visual-search",
    ),
    path(
        "products/visual-search/click/",
        VisualProductClickView.as_view(),
        name="visual-product-click",
    ),
    path(
        "products/suggestions/", ProductSearchSuggestionsView.as_view(), name="product-suggestions"
    ),
    path(
        "products/<slug:slug>/similar/",
        ProductSimilarView.as_view(),
        name="product-similar",
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
    # Phase 17: the notification center (session-authenticated, customer-scoped; writes
    # are throttled by DRF scope and the sliding-window NotificationThrottle).
    path("notifications/", NotificationListView.as_view(), name="notification-list"),
    path(
        "notifications/read-all/",
        NotificationReadAllView.as_view(),
        name="notification-read-all",
    ),
    path(
        "notifications/preferences/",
        NotificationPreferencesView.as_view(),
        name="notification-preferences",
    ),
    path(
        "notifications/<int:pk>/read/",
        NotificationReadView.as_view(),
        name="notification-read",
    ),
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
    # Phase 21: Style Studio API (the deterministic generator behind the studio pages;
    # the complete-look mirror is public because the PDP widget it answers is public).
    path("studio/outfit/", StudioGenerateView.as_view(), name="studio-outfit"),
    path("studio/outfit/save/", StudioSaveView.as_view(), name="studio-outfit-save"),
    path("studio/goal/", StudioGoalView.as_view(), name="studio-goal"),
    path(
        "studio/complete-look/<slug:slug>/",
        StudioCompleteLookView.as_view(),
        name="studio-complete-look",
    ),
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
    # Phase 14: quests and rewards (session-authenticated, customer-scoped; progress is
    # server-derived and the start action takes no input).
    path("quests/", QuestListAPIView.as_view(), name="quest-list"),
    path("quests/<slug:slug>/", QuestDetailAPIView.as_view(), name="quest-detail"),
    path("quests/<slug:slug>/start/", QuestStartAPIView.as_view(), name="quest-start"),
    path("rewards/", RewardsAPIView.as_view(), name="rewards"),
    # Phase 15: FLASH Support & Customer Care (session-authenticated; the customer's own
    # tickets at /support/tickets/, the desk's queue under /support/staff/).
    path("support/", SupportRootView.as_view(), name="support-root"),
    path("support/tickets/", SupportTicketListCreateView.as_view(), name="support-ticket-list"),
    path(
        "support/tickets/<str:number>/",
        SupportTicketDetailView.as_view(),
        name="support-ticket-detail",
    ),
    path(
        "support/tickets/<str:number>/messages/",
        SupportTicketMessagesView.as_view(),
        name="support-ticket-message-list",
    ),
    path(
        "support/tickets/<str:number>/close/",
        SupportTicketCloseView.as_view(),
        name="support-ticket-close",
    ),
    path(
        "support/tickets/<str:number>/reopen/",
        SupportTicketReopenView.as_view(),
        name="support-ticket-reopen",
    ),
    path(
        "support/staff/tickets/",
        StaffTicketListView.as_view(),
        name="support-staff-ticket-list",
    ),
    path(
        "support/staff/tickets/<str:number>/",
        StaffTicketDetailView.as_view(),
        name="support-staff-ticket-detail",
    ),
    path(
        "support/staff/tickets/<str:number>/assign/",
        StaffTicketAssignView.as_view(),
        name="support-staff-ticket-assign",
    ),
    path(
        "support/staff/tickets/<str:number>/status/",
        StaffTicketStatusView.as_view(),
        name="support-staff-ticket-status",
    ),
    path(
        "support/staff/tickets/<str:number>/priority/",
        StaffTicketPriorityView.as_view(),
        name="support-staff-ticket-priority",
    ),
    path(
        "support/staff/tickets/<str:number>/escalate/",
        StaffTicketEscalateView.as_view(),
        name="support-staff-ticket-escalate",
    ),
    path(
        "support/staff/tickets/<str:number>/link/",
        StaffTicketLinkView.as_view(),
        name="support-staff-ticket-link",
    ),
    path(
        "support/staff/tickets/<str:number>/note/",
        StaffTicketNoteView.as_view(),
        name="support-staff-ticket-note",
    ),
    path(
        "support/staff/tickets/<str:number>/reply/",
        StaffTicketReplyView.as_view(),
        name="support-staff-ticket-reply",
    ),
    # Phase 16: FLASH Operations (session-authenticated; every route answers 404 to a
    # caller without the matching back-office capability).
    path("backoffice/", BackOfficeRootView.as_view(), name="backoffice-root"),
    path(
        "backoffice/dashboard/",
        BackOfficeDashboardView.as_view(),
        name="backoffice-dashboard",
    ),
    path("backoffice/alerts/", BackOfficeAlertsView.as_view(), name="backoffice-alerts"),
    path("backoffice/orders/", BackOfficeOrdersView.as_view(), name="backoffice-orders"),
    path("backoffice/audit/", BackOfficeAuditView.as_view(), name="backoffice-audit"),
]
