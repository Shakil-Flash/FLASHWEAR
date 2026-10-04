"""Creator URLs (Phase 12)."""

from django.urls import path

from . import views

app_name = "creator"

urlpatterns = [
    # Creator profile
    path("", views.creator_list, name="creator_list"),
    path("<slug:slug>/", views.creator_detail, name="creator_detail"),

    # Creator profile (authenticated/dashboard)
    path("me/", views.creator_me, name="creator_me"),
    path("me/posts/", views.creator_me_posts, name="creator_me_posts"),

    # Creator posts
    path("posts/<slug:slug>/", views.post_detail, name="post_detail"),
    path("posts/<slug:slug>/like/", views.post_like, name="post_like"),
    path("posts/<slug:slug>/save/", views.post_save, name="post_save"),
    path("posts/<slug:slug>/report/", views.post_report, name="post_report"),

    # Creator discovery
    path("inspiration/", views.inspiration_list, name="inspiration_list"),
    path("inspiration/featured/", views.inspiration_featured, name="inspiration_featured"),
    path("inspiration/newest/", views.inspiration_newest, name="inspiration_newest"),

    # API v1 endpoints
    path("api/v1/", views.api_v1, name="api_v1"),
]