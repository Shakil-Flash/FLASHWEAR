"""Core views (Django templates).

Phase 1 keeps view logic deliberately thin. Business logic belongs in per-app services
from Phase 2 onwards; these views only orchestrate the request/response cycle.
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.core.context_processors import get_site_configuration


@never_cache
@require_GET
def health_view(request: HttpRequest) -> JsonResponse:
    """Liveness probe for load balancers, Docker and uptime monitoring.

    Returns a fixed payload only; no database, cache or credential details are exposed,
    so the endpoint is safe to make public.
    """
    return JsonResponse({"status": "ok"})


def home(request: HttpRequest) -> HttpResponse:
    """Homepage: catalogue highlights above the fold, platform status below.

    The catalogue queries are cheap and eager-loaded (one query per strip, capped at four and
    eight rows), so the homepage costs a fixed handful of queries no matter how large the catalogue
    grows. An empty catalogue renders an honest empty state rather than a broken grid.
    """
    site = get_site_configuration()

    user = getattr(request, "user", None)
    is_team_member = bool(user and user.is_authenticated and user.is_staff)
    if site.maintenance_mode and not is_team_member:
        return render(request, "pages/maintenance.html", status=503)

    from apps.catalog import selectors

    context = {
        "seo_title": f"{site.site_name} — {site.tagline}",
        "seo_description": site.default_seo_description,
        "is_development": settings.DEBUG,
        "nav_categories": selectors.storefront_categories(limit=6),
        "featured_products": selectors.homepage_featured(limit=4),
        "new_arrivals": selectors.homepage_new_arrivals(limit=8),
        "featured_collections": list(selectors.live_collections(featured_only=True)[:3]),
    }
    return render(request, "pages/home.html", context)


def page_not_found(request: HttpRequest, exception) -> HttpResponse:
    """404 handler that uses the global error template."""
    return render(request, "pages/errors/404.html", {"exception": exception}, status=404)


def server_error(request: HttpRequest) -> HttpResponse:
    """500 handler that uses the global error template."""
    return render(request, "pages/errors/500.html", status=500)
