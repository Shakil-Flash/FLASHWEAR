"""Core URL configuration (storefront pages)."""

from django.contrib.sitemaps.views import sitemap
from django.urls import path

from apps.catalog.sitemaps import sitemaps
from apps.core import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("robots.txt", views.robots, name="robots"),
    path("sitemap.xml", sitemap, {"sitemaps": sitemaps}, name="sitemap"),
    path("health/", views.health_view, name="health"),
    path("health/live/", views.live_view, name="health_live"),
    path("health/ready/", views.ready_view, name="health_ready"),
    path("content/track/", views.track_content_interaction, name="track_content_interaction"),
]

