"""Engagement URLs, mounted at the site root by :mod:`config.urls`.

Review *creation* lives on the product page (``/products/<slug>/review/`` under the
``catalog`` namespace, because that is the page the form posts from); edit and delete are
standalone ``/reviews/<pk>/...`` routes here. The FLASH Points dashboard is registered under
``/account/`` by :mod:`apps.accounts.account_urls` so it inherits the account area's
navigation and ``login_required`` wiring -- one mount per page, no aliases.
"""

from django.urls import path

from apps.engagement import views

app_name = "engagement"

urlpatterns = [
    path("reviews/<int:pk>/edit/", views.review_edit, name="review-edit"),
    path("reviews/<int:pk>/delete/", views.review_delete, name="review-delete"),
]
