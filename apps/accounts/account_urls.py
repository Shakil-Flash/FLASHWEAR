"""Phase 2 customer account URLs, mounted at ``/account/``.

Split from :mod:`apps.accounts.urls` because a Django URL module declares exactly one
``app_name``. The Phase 1 ``accounts:`` namespace stays exactly where it was.

``/account/login/`` and ``/account/logout/`` are aliases of the same view objects used by the
Phase 1 mount, so a customer who bookmarked either path gets identical throttling, CSRF and
expiry behaviour -- one implementation, no drift.

Everything except the two aliases, registration, the emailed links and the post-closure page
requires a signed-in customer; that is applied once at the bottom of the module rather than
decorated onto every view by hand.
"""

from django.contrib.auth.decorators import login_required
from django.urls import path

from apps.accounts import views
from apps.closet import views as closet_views
from apps.engagement.views import loyalty_dashboard
from apps.orders.views import OrderDetailView, OrderListView, order_cancel

app_name = "account"

# Anonymous-reachable: the customer either has no session yet, or carries the only credential
# they need (a signed link in an email, or the confirmation that follows closure).
urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path(
        "verify-email/<str:uidb64>/<str:token>/",
        views.VerifyEmailView.as_view(),
        name="verify-email",
    ),
    path("password/reset/", views.PasswordResetView.as_view(), name="password-reset"),
    path(
        "password/reset/done/",
        views.PasswordResetDoneView.as_view(),
        name="password-reset-done",
    ),
    path(
        "password/reset/confirm/<str:uidb64>/<str:token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path(
        "password/reset/complete/",
        views.PasswordResetCompleteView.as_view(),
        name="password-reset-complete",
    ),
    path("deleted/", views.AccountDeletedView.as_view(), name="deleted"),
]


# Signed-in only. ``login_required`` defaults to ``settings.LOGIN_URL`` and appends ``?next=``, so
# an interrupted customer resumes where they meant to be.
def protected(route, view, name):
    """Register ``view`` behind ``login_required``."""
    return path(route, login_required(view), name=name)


urlpatterns += [
    protected("", views.AccountDashboardView.as_view(), "dashboard"),
    protected("profile/", views.ProfileUpdateView.as_view(), "profile"),
    protected("security/", views.SecurityView.as_view(), "security"),
    protected("password/change/", views.PasswordChangeView.as_view(), "password-change"),
    protected(
        "verify-email/resend/",
        views.ResendVerificationEmailView.as_view(),
        "resend-verification",
    ),
    protected("addresses/", views.AddressListView.as_view(), "addresses"),
    protected("addresses/new/", views.AddressCreateView.as_view(), "address-create"),
    protected("addresses/<int:pk>/edit/", views.AddressUpdateView.as_view(), "address-update"),
    protected("addresses/<int:pk>/delete/", views.AddressDeleteView.as_view(), "address-delete"),
    protected(
        "addresses/<int:pk>/default/<str:kind>/",
        views.AddressSetDefaultView.as_view(),
        "address-set-default",
    ),
    protected("delete/", views.AccountDeleteView.as_view(), "delete"),
    # Phase 6: order history (views live in apps.orders; the account area owns the URLs).
    protected("orders/", OrderListView.as_view(), "orders"),
    protected("orders/<str:number>/", OrderDetailView.as_view(), "order-detail"),
    protected("orders/<str:number>/cancel/", order_cancel, "order-cancel"),
    # Phase 7: FLASH Points dashboard (view lives in apps.engagement).
    protected("loyalty/", loyalty_dashboard, "loyalty"),
    # Phase 8: FLASH Closet and the outfit builder (views live in apps.closet).
    protected("closet/", closet_views.closet_page, "closet"),
    protected("closet/new/", closet_views.item_create, "closet-item-create"),
    protected("closet/purchases/", closet_views.purchases_page, "closet-purchases"),
    protected(
        "closet/purchases/<int:pk>/add/",
        closet_views.item_add_purchase,
        "closet-add-purchase",
    ),
    protected("closet/<int:pk>/edit/", closet_views.item_edit, "closet-item-edit"),
    protected("closet/<int:pk>/archive/", closet_views.item_archive, "closet-item-archive"),
    protected("closet/<int:pk>/restore/", closet_views.item_restore, "closet-item-restore"),
    protected("closet/<int:pk>/delete/", closet_views.item_delete, "closet-item-delete"),
    protected("outfits/", closet_views.outfits_page, "outfits"),
    protected("outfits/new/", closet_views.outfit_create, "outfit-create"),
    protected("outfits/<int:pk>/", closet_views.outfit_detail, "outfit-detail"),
    protected("outfits/<int:pk>/update/", closet_views.outfit_update, "outfit-update"),
    protected("outfits/<int:pk>/save/", closet_views.outfit_save, "outfit-save"),
    protected("outfits/<int:pk>/archive/", closet_views.outfit_archive, "outfit-archive"),
    protected("outfits/<int:pk>/restore/", closet_views.outfit_restore, "outfit-restore"),
    protected(
        "outfits/<int:pk>/duplicate/",
        closet_views.outfit_duplicate,
        "outfit-duplicate",
    ),
    protected("outfits/<int:pk>/delete/", closet_views.outfit_delete, "outfit-delete"),
    protected(
        "outfits/<int:pk>/items/add/",
        closet_views.outfit_item_add,
        "outfit-item-add",
    ),
    protected(
        "outfits/<int:pk>/items/<int:item_id>/remove/",
        closet_views.outfit_item_remove,
        "outfit-item-remove",
    ),
    protected(
        "outfits/<int:pk>/items/<int:item_id>/move/",
        closet_views.outfit_item_move,
        "outfit-item-move",
    ),
]
