"""FLASH Loop public URLs (``/loop/``).

Mounted from ``config.urls``. The account-area routes live in
:mod:`apps.accounts.account_urls` under ``/account/loop/`` (one URL
module declares exactly one ``app_name``).
"""

from django.urls import path

from apps.loop import views

app_name = "loop"

urlpatterns = [
    path("", views.resale_list, name="resale-list"),
    path("resale/", views.resale_list, name="resale-shelf"),
    path("resale/<slug:slug>/", views.resale_detail, name="resale-detail"),
]
