"""Core URL configuration (storefront pages)."""

from django.urls import path

from apps.core import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("health/", views.health_view, name="health"),
]
