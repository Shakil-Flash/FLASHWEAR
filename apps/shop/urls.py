"""Shop app URLs.

Namespace: ``shop``
"""

from django.urls import path

from apps.shop import checkout_views, views

app_name = "shop"

urlpatterns = [
    # Cart
    path("cart/", views.cart_detail, name="cart"),
    path("cart/add/", views.cart_add, name="cart-add"),
    path("cart/update/<int:item_pk>/", views.cart_update, name="cart-update"),
    path("cart/remove/<int:item_pk>/", views.cart_remove, name="cart-remove"),
    path("cart/clear/", views.cart_clear, name="cart-clear"),
    # Wishlist
    path("wishlist/", views.wishlist_detail, name="wishlist"),
    path("wishlist/add/", views.wishlist_add, name="wishlist-add"),
    path("wishlist/remove/<int:item_pk>/", views.wishlist_remove, name="wishlist-remove"),
    path(
        "wishlist/move-to-bag/<int:item_pk>/",
        views.wishlist_move_to_bag,
        name="wishlist-move-to-bag",
    ),
    path(
        "wishlist/toggle-alert/<int:item_pk>/",
        views.wishlist_toggle_alert,
        name="wishlist-toggle-alert",
    ),
    path(
        "wishlist/add-all-to-bag/",
        views.wishlist_add_all_to_bag,
        name="wishlist-add-all-to-bag",
    ),
    path(
        "wishlist/to-product/<int:item_pk>/",
        views.wishlist_to_product,
        name="wishlist-to-product",
    ),
    # Checkout (Phase 5 validates; Phase 6 places the order and pays)
    path("checkout/", checkout_views.checkout_detail, name="checkout"),
    path("checkout/address/", checkout_views.checkout_address, name="checkout-address"),
    path("checkout/shipping/", checkout_views.checkout_shipping, name="checkout-shipping"),
    path("checkout/validate/", checkout_views.checkout_validate, name="checkout-validate"),
    # Phase 7: discounts (engine validates; nothing persists that the engine rejected)
    path(
        "checkout/promotion/",
        checkout_views.checkout_promotion,
        name="checkout-promotion",
    ),
    path(
        "checkout/loyalty/",
        checkout_views.checkout_loyalty,
        name="checkout-loyalty",
    ),
    path("checkout/place/", checkout_views.checkout_place, name="checkout-place"),
    path(
        "checkout/payment/<str:number>/",
        checkout_views.checkout_payment,
        name="checkout-payment",
    ),
    path(
        "checkout/payment/<str:number>/retry/",
        checkout_views.checkout_payment_retry,
        name="checkout-payment-retry",
    ),
    path(
        "checkout/done/<str:number>/",
        checkout_views.checkout_done,
        name="checkout-done",
    ),
    # HTMX / AJAX endpoints
    path("cart/count/", views.cart_count, name="cart-count"),
    path("cart/mini/", views.cart_mini, name="cart-mini"),
    path("wishlist/count/", views.wishlist_count, name="wishlist-count"),
    path("cart/totals/", views.cart_totals, name="cart-totals"),
]
