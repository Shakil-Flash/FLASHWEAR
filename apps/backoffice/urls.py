"""URLs for the operations platform (``/operations/``).

Names here are the ones :data:`apps.backoffice.permissions.NAVIGATION` points at, so the
sidebar, the dashboard tiles and the alert deep links all resolve to a route that exists.
Order matters only where a bare segment could be read as a parameter: ``products/bulk/``
is declared before ``products/<pk>/`` would ever be (there is no product detail route --
the desk reads products through the catalogue itself).
"""

from __future__ import annotations

from django.urls import path

from apps.backoffice import views

app_name = "backoffice"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("alerts/", views.alerts, name="alerts"),
    path("health/", views.health, name="health"),
    # Phase 20: the funnel (aggregates only, gated by analytics.view)
    path("analytics/", views.analytics, name="analytics"),
    # Notifications (Phase 17)
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/<int:pk>/", views.notification_detail, name="notification_detail"),
    path("notifications/<int:pk>/retry/", views.notification_retry, name="notification_retry"),
    # Sales
    path("orders/", views.orders, name="orders"),
    path("orders/<str:number>/", views.order_detail, name="order_detail"),
    path("orders/<str:number>/action/", views.order_action, name="order_action"),
    path("orders/<str:number>/note/", views.order_note, name="order_note"),
    path("payments/", views.payments, name="payments"),
    path("payments/<int:pk>/", views.payment_detail, name="payment_detail"),
    path("shipments/", views.shipments, name="shipments"),
    path("customers/", views.customers, name="customers"),
    # Merchandising
    path("products/", views.products, name="products"),
    path("products/bulk/", views.products_bulk, name="products_bulk"),
    path("inventory/", views.inventory, name="inventory"),
    path("inventory/adjust/", views.adjust_stock, name="inventory_adjust"),
    path("drops/", views.drops, name="drops"),
    path("promotions/", views.promotions, name="promotions"),
    path("quests/", views.quests, name="quests"),
    # Community
    path("reviews/", views.reviews, name="reviews"),
    path("reviews/bulk/", views.reviews_bulk, name="reviews_bulk"),
    path("loop/", views.loop, name="loop"),
    path("loop/<int:pk>/action/", views.loop_action, name="loop_action"),
    path("support/", views.support, name="support"),
    path("support/<str:number>/assign/", views.support_assign, name="support_assign"),
    path("support/<str:number>/status/", views.support_status, name="support_status"),
    path("points/", views.points, name="points"),
    path("points/adjust/", views.points_adjust, name="points_adjust"),
    # Governance
    path("audit/", views.audit, name="audit"),
    path("staff/", views.staff, name="staff"),
    path("staff/<int:pk>/update/", views.staff_update, name="staff_update"),
]
