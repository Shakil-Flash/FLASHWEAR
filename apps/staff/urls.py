"""URL configuration for the FLASHWEAR Staff Portal."""

from django.urls import path

from apps.staff import views

app_name = "staff"

urlpatterns = [
    path("", views.dashboard_view, name="dashboard"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("access-denied/", views.access_denied_view, name="access_denied"),
    # Account Management
    path("accounts/", views.account_list_view, name="account_list"),
    path("accounts/create/", views.account_create_view, name="account_create"),
    path("accounts/<int:pk>/", views.account_detail_view, name="account_detail"),
    path("accounts/<int:pk>/roles/", views.account_roles_view, name="account_roles"),
    path(
        "accounts/<int:pk>/toggle-active/",
        views.account_toggle_active_view,
        name="account_toggle_active",
    ),
    path("audit/", views.audit_logs_view, name="audit_logs"),
    # Operational Modules (Connecting to Existing Data)
    path("products/", views.products_view, name="products"),
    path("inventory/", views.inventory_view, name="inventory"),
    path("orders/", views.orders_view, name="orders"),
    path("orders/<int:pk>/", views.order_detail_view, name="order_detail"),
    path(
        "orders/<int:pk>/packing-slip/",
        views.order_packing_slip_view,
        name="order_packing_slip",
    ),
    path(
        "orders/<int:pk>/process/",
        views.order_process_action_view,
        name="order_process_action",
    ),
    path(
        "orders/<int:pk>/pick/",
        views.order_start_picking_action_view,
        name="order_start_picking_action",
    ),
    path(
        "orders/<int:pk>/pack/",
        views.order_mark_packed_action_view,
        name="order_mark_packed_action",
    ),
    path(
        "orders/<int:pk>/dispatch/",
        views.order_dispatch_action_view,
        name="order_dispatch_action",
    ),
    path("deliveries/", views.deliveries_view, name="deliveries"),
    path("support/", views.support_view, name="support"),
    path("reports/", views.reports_view, name="reports"),
]
