"""Root URL configuration.

Six public surfaces:

* ``/``             storefront pages (Django templates, Tailwind, HTMX, Alpine.js). The catalogue
  is mounted here rather than under ``/catalog/`` because ``/products/<slug>/`` is a shopper URL,
  not an implementation detail.
* ``/accounts/``    Phase 1 sign in / sign out (``LOGIN_URL`` points here)
* ``/account/``     Phase 2 customer account area (dashboard, profile, addresses, security)
* ``/api/v1/``      versioned REST API consumed by web, mobile and future frontends
* ``/admin/``       Django admin back office
* ``/payments/``    Phase 6 provider webhook and local payment controls (``/shop/`` holds the
  storefront checkout flow itself)
* ``/operations/``  Phase 16 FLASH Operations -- the staff back office (capability-gated;
  every route answers 404 to anyone without one)
* ``/notifications/`` Phase 17 notification center, read-state endpoints and the
  opaque-token one-click unsubscribe landing page
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

# ``config.api.v1.urls`` declares ``app_name = "v1"``, so the namespace comes from
# the module itself; adding ``namespace=`` here would produce ``v1:v1``.
urlpatterns = [
    # Catalogue first: it owns the site root patterns (``/products/``, ``/collections/``) and would
    # otherwise be shadowed by ``apps.core.urls``' catch-all home page.
    path("", include("apps.catalog.urls")),
    path("", include("apps.core.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("account/", include("apps.accounts.account_urls")),
    path("api/v1/", include("config.api.v1.urls")),
    path("admin/", admin.site.urls),
    # Phase 5: Shopping cart and wishlist
    path("shop/", include("apps.shop.urls")),
    # Phase 6: payments (webhook + development controls)
    path("payments/", include("apps.payments.urls")),
    # Phase 7: review edit/delete (the FLASH Points dashboard mounts under /account/)
    path("", include("apps.engagement.urls")),
    # Phase 13: FLASH Loop public resale shelf (/loop/, /loop/resale/<slug>/)
    path("loop/", include("apps.loop.urls")),
    # Phase 15: FLASH Support & Customer Care (/support/, /support/staff/)
    path("support/", include("apps.support.urls")),
    # Phase 16: FLASH Operations back office (/operations/, /operations/orders/ ...)
    path("operations/", include("apps.backoffice.urls")),
    # Phase 17: notification center (/notifications/, /notifications/unsubscribe/<token>/)
    path("notifications/", include("apps.notifications.urls")),
    # Phase 32: Community, Social Proof & Shoppable UGC (/creators/, /inspiration/)
    path("creators/", include("apps.creator.urls")),
    path("inspiration/", include("apps.creator.inspiration_urls")),
]

# Serve user-uploaded media files in development and container deployments without S3.
if settings.DEBUG or not getattr(settings, "AWS_STORAGE_BUCKET_NAME", None):
    import re

    from django.urls import re_path
    from django.views.static import serve

    media_prefix = re.escape(settings.MEDIA_URL.lstrip("/"))
    urlpatterns += [
        re_path(
            rf"^{media_prefix}(?P<path>.*)$",
            serve,
            {"document_root": str(settings.MEDIA_ROOT)},
        ),
    ]

handler400 = "apps.core.views.bad_request"
handler403 = "apps.core.views.permission_denied"
handler404 = "apps.core.views.page_not_found"
handler500 = "apps.core.views.server_error"
