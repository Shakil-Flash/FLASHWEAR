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
    # Inventory & Stock Operations (Phase 62)
    path("inventory/", views.inventory_view, name="inventory"),
    path(
        "inventory/<int:variant_id>/",
        views.inventory_variant_detail_view,
        name="inventory_variant_detail",
    ),
    path(
        "inventory/<int:variant_id>/adjust/",
        views.inventory_adjust_action_view,
        name="inventory_adjust_action",
    ),
    path(
        "inventory/<int:variant_id>/receive/",
        views.inventory_receive_action_view,
        name="inventory_receive_action",
    ),
    path(
        "inventory/<int:variant_id>/threshold/",
        views.inventory_threshold_action_view,
        name="inventory_threshold_action",
    ),
    path("inventory/movements/", views.inventory_ledger_view, name="inventory_ledger"),
    path(
        "inventory/movements/<int:movement_id>/reverse/",
        views.inventory_reverse_action_view,
        name="inventory_reverse_action",
    ),
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
    path("deliveries/<int:pk>/", views.delivery_detail_view, name="delivery_detail"),
    path(
        "deliveries/<int:pk>/assign/",
        views.delivery_assign_action_view,
        name="delivery_assign_action",
    ),
    path(
        "deliveries/<int:pk>/status/",
        views.delivery_status_action_view,
        name="delivery_status_action",
    ),
    path(
        "deliveries/<int:pk>/attempt/",
        views.delivery_attempt_action_view,
        name="delivery_attempt_action",
    ),
    path(
        "deliveries/<int:pk>/pod/",
        views.delivery_pod_action_view,
        name="delivery_pod_action",
    ),
    path(
        "deliveries/<int:pk>/otp/generate/",
        views.delivery_otp_generate_action_view,
        name="delivery_otp_generate_action",
    ),
    path("support/", views.support_view, name="support"),
    path("reports/", views.reports_view, name="reports"),
    # Staff Tasks & Collaboration (Phase 63)
    path("tasks/", views.task_list_view, name="task_list"),
    path("tasks/create/", views.task_create_view, name="task_create"),
    path("tasks/<int:pk>/", views.task_detail_view, name="task_detail"),
    path("tasks/<int:pk>/edit/", views.task_edit_view, name="task_edit"),
    path("tasks/<int:pk>/assign/", views.task_assign_action_view, name="task_assign_action"),
    path("tasks/<int:pk>/status/", views.task_status_action_view, name="task_status_action"),
    path("tasks/<int:pk>/progress/", views.task_progress_action_view, name="task_progress_action"),
    path("tasks/<int:pk>/comment/", views.task_comment_action_view, name="task_comment_action"),
    # Staff Notifications (Phase 63)
    path("notifications/", views.notification_list_view, name="notification_list"),
    path(
        "notifications/<int:pk>/read/",
        views.notification_read_action_view,
        name="notification_read_action",
    ),
    path(
        "notifications/mark-all-read/",
        views.notification_mark_all_read_action_view,
        name="notification_mark_all_read_action",
    ),
]
