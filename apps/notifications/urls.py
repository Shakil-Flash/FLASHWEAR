"""URLs for the notification center, mounted at ``/notifications/``.

Every view is behind ``login_required`` (except the token-driven unsubscribe, whose token
*is* the capability), so the routes register plainly -- the decorator is the boundary.
``/account/notifications/`` (preferences) lives in the account URLconf, next to the rest
of the account area, and is reached as ``account:notifications``.
"""

from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.center, name="center"),
    path("read-all/", views.read_all, name="read-all"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/action/", views.action_click, name="action-click"),
    # Last on purpose: a bare /notifications/unsubscribe/<garbage>/ must not shadow
    # anything, and the token is opaque enough that no other route can look like it.
    path("unsubscribe/<str:token>/", views.unsubscribe, name="unsubscribe"),
]
