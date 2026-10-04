"""Recommendation APIs URL patterns (Phase 10).

Namespace: ``v1`` (registered in ``config/api/v1/urls.py``).
"""

from django.urls import path

from apps.recommendations.api import (
    ClosetComplementView,
    FeedbackView,
    ForYouView,
    NewForYouView,
    OutfitCompletionView,
    RecommendationRootView,
    SimilarProductsView,
)

app_name = "v1"

urlpatterns = [
    # Root: list available recommendation contexts
    path("", RecommendationRootView.as_view(), name="recommendations-root"),

    # Personalized recommendations (the primary endpoint)
    path("for-you/", ForYouView.as_view(), name="for-you"),

    # Closet complement: products that complement what the user already owns
    path("closet/", ClosetComplementView.as_view(), name="closet-complement"),

    # Outfit completion: recommend products to complete a saved outfit
    path("outfit/<int:outfit_id>/", OutfitCompletionView.as_view(), name="outfit-completion"),

    # Similar products to a given product (via slug)
    path("similar/", SimilarProductsView.as_view(), name="similar-products"),

    # New products matching user preferences
    path("new-for-you/", NewForYouView.as_view(), name="new-for-you"),

    # Feedback: submit user interest/disinterest on a recommendation
    path("feedback/", FeedbackView.as_view(), name="feedback"),
]
