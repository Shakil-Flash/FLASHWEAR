"""Phase 1 account URLs -- sign in and sign out.

Mounted at ``/accounts/`` by ``config.urls``. These names are load-bearing: ``LOGIN_URL`` resolves
here so that ``login_required`` can redirect without guessing, and the Phase 1 test suite asserts
this path. The Phase 2 surface lives in :mod:`apps.accounts.account_urls`.
"""

from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
]
