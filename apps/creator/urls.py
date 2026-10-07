"""Creator URLs (Phase 12 / Phase 32)."""

from django.urls import path

from . import views

app_name = "creator"

urlpatterns = [
    # Creator directory & profile
    path("", views.creator_list, name="creator_list"),
    path("apply/", views.creator_apply, name="creator_apply"),
    path("me/", views.creator_me, name="creator_me"),
    path("me/posts/", views.creator_me_posts, name="creator_me_posts"),
    path("<slug:slug>/", views.creator_detail, name="creator_detail"),
    # Creator posts
    path("posts/<slug:slug>/", views.post_detail, name="post_detail"),
    path("posts/<slug:slug>/like/", views.post_like, name="post_like"),
    path("posts/<slug:slug>/save/", views.post_save, name="post_save"),
    path("posts/<slug:slug>/report/", views.post_report, name="post_report"),
    # Community inspiration feed alias
    path("inspiration/", views.inspiration_list, name="inspiration_list"),
]
