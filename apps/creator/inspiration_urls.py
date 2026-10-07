"""Inspiration and Community URLs (Phase 32)."""

from django.urls import path

from . import views

app_name = "inspiration"

urlpatterns = [
    path("", views.inspiration_list, name="list"),
    path("<slug:slug>/", views.post_detail, name="detail"),
    path("<slug:slug>/like/", views.post_like, name="like"),
    path("<slug:slug>/save/", views.post_save, name="save"),
    path("<slug:slug>/report/", views.post_report, name="report"),
]
