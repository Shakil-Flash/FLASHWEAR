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
    # Phase 20 / Phase 36: analytics and reporting export (gated by analytics.view)
    path("analytics/", views.analytics, name="analytics"),
    path("analytics/export/", views.export_analytics_csv, name="analytics_export"),
    # Notifications (Phase 17)
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/<int:pk>/", views.notification_detail, name="notification_detail"),
    path("notifications/<int:pk>/retry/", views.notification_retry, name="notification_retry"),
    # Sales
    path("orders/", views.orders, name="orders"),
    path("orders/<str:number>/", views.order_detail, name="order_detail"),
    path("orders/<str:number>/action/", views.order_action, name="order_action"),
    path("orders/<str:number>/note/", views.order_note, name="order_note"),
    path("returns/", views.returns, name="returns"),
    path("returns/<str:number>/", views.return_detail, name="return_detail"),
    path("returns/<str:number>/action/", views.return_action, name="return_action"),
    path("payments/", views.payments, name="payments"),
    path("payments/<int:pk>/", views.payment_detail, name="payment_detail"),
    path("shipments/", views.shipments, name="shipments"),
    path("customers/", views.customers, name="customers"),
    # Content Studio (Phase 29)
    path("content/homepage/", views.content_homepage, name="content_homepage"),
    path("content/homepage/add/", views.content_homepage_edit, name="content_homepage_add"),
    path(
        "content/homepage/<int:pk>/edit/",
        views.content_homepage_edit,
        name="content_homepage_edit",
    ),
    path(
        "content/homepage/<int:pk>/toggle/",
        views.content_homepage_toggle,
        name="content_homepage_toggle",
    ),
    path(
        "content/homepage/<int:pk>/delete/",
        views.content_homepage_delete,
        name="content_homepage_delete",
    ),
    path("content/preview/", views.content_preview, name="content_preview"),
    path("content/editorial/", views.content_editorial, name="content_editorial"),
    path("content/editorial/add/", views.content_editorial_edit, name="content_editorial_add"),
    path(
        "content/editorial/<int:pk>/edit/",
        views.content_editorial_edit,
        name="content_editorial_edit",
    ),
    path(
        "content/editorial/<int:pk>/delete/",
        views.content_editorial_delete,
        name="content_editorial_delete",
    ),
    path("content/campaigns/", views.content_campaigns, name="content_campaigns"),
    path("content/campaigns/add/", views.content_campaign_edit, name="content_campaign_add"),
    path(
        "content/campaigns/<int:pk>/edit/",
        views.content_campaign_edit,
        name="content_campaign_edit",
    ),
    path(
        "content/campaigns/<int:pk>/delete/",
        views.content_campaign_delete,
        name="content_campaign_delete",
    ),
    # Merchandising Studio (Phase 29)
    path(
        "merchandising/featured-products/",
        views.merch_featured_products,
        name="merch_featured_products",
    ),
    path(
        "merchandising/featured-products/<int:section_pk>/add/",
        views.merch_product_add,
        name="merch_product_add",
    ),
    path(
        "merchandising/featured-products/<int:section_pk>/reorder/",
        views.merch_product_reorder,
        name="merch_product_reorder",
    ),
    path(
        "merchandising/featured-products/<int:section_pk>/remove/<int:product_pk>/",
        views.merch_product_remove,
        name="merch_product_remove",
    ),
    path(
        "merchandising/featured-collections/",
        views.merch_featured_collections,
        name="merch_featured_collections",
    ),
    path("merchandising/scheduled/", views.merch_scheduled, name="merch_scheduled"),
    # Merchandising
    path("products/", views.products, name="products"),
    path("products/bulk/", views.products_bulk, name="products_bulk"),
    path("products/health/", views.catalog_health_report, name="catalog_health"),
    path("products/health/export/", views.export_catalog_health_csv, name="catalog_health_export"),
    path("products/remediate/", views.catalog_remediate, name="catalog_remediate"),
    path("inventory/", views.inventory, name="inventory"),
    path("inventory/adjust/", views.adjust_stock, name="inventory_adjust"),
    path("inventory/health/", views.inventory_health, name="inventory_health"),
    path("inventory/health/export/", views.export_inventory_health_csv, name="inventory_health_export"),
    path("inventory/restock/", views.inventory_restock, name="inventory_restock"),
    path("inventory/reconciliation/", views.inventory_reconciliation, name="inventory_reconciliation"),
    path("inventory/reconcile-action/", views.inventory_reconcile_action, name="inventory_reconcile_action"),
    # Procurement & Purchasing (Phase 45)
    path("procurement/", views.procurement_dashboard, name="procurement_dashboard"),
    path("procurement/suppliers/", views.suppliers_list, name="suppliers"),
    path("procurement/suppliers/new/", views.supplier_create, name="supplier_create"),
    path("procurement/suppliers/<int:pk>/", views.supplier_detail, name="supplier_detail"),
    path("procurement/suppliers/<int:pk>/edit/", views.supplier_edit, name="supplier_edit"),
    path("procurement/orders/", views.purchase_orders_list, name="purchase_orders"),
    path("procurement/orders/new/", views.purchase_order_create, name="purchase_order_create"),
    path("procurement/orders/<int:pk>/", views.purchase_order_detail, name="purchase_order_detail"),
    path("procurement/orders/<int:pk>/edit/", views.purchase_order_edit, name="purchase_order_edit"),
    path("procurement/orders/<int:pk>/action/", views.purchase_order_action, name="purchase_order_action"),
    path("procurement/orders/<int:pk>/receive/", views.purchase_order_receive, name="purchase_order_receive"),
    path("procurement/orders/<int:pk>/print/", views.purchase_order_print, name="purchase_order_print"),
    # Accounts Payable & Supplier Invoices (Phase 46)
    path("procurement/ap/", views.accounts_payable_dashboard, name="accounts_payable_dashboard"),
    path("procurement/invoices/", views.supplier_invoices_list, name="supplier_invoices"),
    path("procurement/invoices/new/", views.supplier_invoice_create, name="supplier_invoice_create"),
    path("procurement/invoices/<int:pk>/", views.supplier_invoice_detail, name="supplier_invoice_detail"),
    path("procurement/invoices/<int:pk>/edit/", views.supplier_invoice_edit, name="supplier_invoice_edit"),
    path("procurement/invoices/<int:pk>/action/", views.supplier_invoice_action, name="supplier_invoice_action"),
    path("procurement/payments/", views.supplier_payments_list, name="supplier_payments"),
    path("procurement/payments/new/", views.supplier_payment_create, name="supplier_payment_create"),
    path("procurement/payments/<int:pk>/reverse/", views.supplier_payment_reverse, name="supplier_payment_reverse"),
    path("procurement/credit-notes/", views.supplier_credit_notes_list, name="supplier_credit_notes"),
    path("procurement/credit-notes/new/", views.supplier_credit_note_create, name="supplier_credit_note_create"),
    path("procurement/credit-notes/<int:pk>/apply/", views.supplier_credit_note_apply, name="supplier_credit_note_apply"),
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
