"""URL routing for Business Automation (Phase 56)."""

from __future__ import annotations

from django.urls import path

from apps.automation import views

urlpatterns = [
    path("", views.rule_list, name="automations"),
    path("new/", views.rule_create, name="automation_create"),
    path("<int:pk>/edit/", views.rule_edit, name="automation_edit"),
    path("<int:pk>/toggle/", views.rule_toggle, name="automation_toggle"),
    path("<int:pk>/run/", views.rule_run, name="automation_run"),
    path("logs/", views.execution_logs, name="automation_logs"),
    path("logs/<int:pk>/", views.log_detail, name="automation_log_detail"),
]
