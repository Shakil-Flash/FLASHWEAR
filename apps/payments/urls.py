"""Payment URLs.

Namespace: ``payments``. Mounted at ``/payments/`` by ``config.urls``.
"""

from django.urls import path

from apps.payments import views

app_name = "payments"

urlpatterns = [
    path("webhook/<str:provider_key>/", views.webhook, name="webhook"),
    path("<int:pk>/simulate/", views.simulate, name="simulate"),
    path("<int:pk>/cancel/", views.cancel, name="cancel"),
]
