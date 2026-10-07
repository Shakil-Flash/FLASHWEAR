"""Core views (Django templates).

Phase 1 keeps view logic deliberately thin. Business logic belongs in per-app services
from Phase 2 onwards; these views only orchestrate the request/response cycle.
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.core.context_processors import get_site_configuration
from apps.core.seo import organization_schema, website_schema


@never_cache
@require_GET
def health_view(request: HttpRequest) -> JsonResponse:
    """Liveness probe for load balancers, Docker and uptime monitoring.

    Returns a fixed payload only; no database, cache or credential details are exposed,
    so the endpoint is safe to make public.
    """
    return JsonResponse({"status": "ok"})


@never_cache
@require_GET
def live_view(request: HttpRequest) -> JsonResponse:
    """Process liveness: answers as long as the worker can serve a request at all.

    Distinct from ``health_view``/``ready_view`` in intent: an orchestrator should
    restart on this one and route traffic on the readiness one.
    """
    return JsonResponse({"status": "alive"})


@never_cache
@require_GET
def ready_view(request: HttpRequest) -> JsonResponse:
    """Readiness: database and cache must answer; the broker is advisory.

    503 tells the load balancer to pull this instance out of rotation while a
    dependency is down, without restarting a perfectly healthy process.
    """
    from apps.core import health

    checks = {
        "database": health.check_database(),
        "cache": health.check_cache(),
        "broker": health.check_broker(),
    }
    ready = checks["database"] == "ok" and checks["cache"] == "ok"
    return JsonResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status=200 if ready else 503,
    )


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

    from apps.catalog import merchandising, selectors

    active_drops = []
    try:
        from apps.drops.models import FlashDrop

        active_drops = list(FlashDrop.objects.live().prefetch_related("products__product")[:3])
    except Exception:
        pass

    loop_highlights = []
    try:
        from apps.loop.models import ResaleListing

        loop_highlights = list(
            ResaleListing.objects.filter(status=ResaleListing.Status.ACTIVE)
            .select_related("loop_item__product", "loop_item__variant")
            .prefetch_related("loop_item__images")
            .order_by("-listed_at")[:4]
        )
    except Exception:
        pass

    continue_shopping = merchandising.get_continue_shopping_items(request, limit=4)
    recently_viewed = merchandising.get_recently_viewed(request, limit=4)
    essentials = list(
        selectors.storefront_products()
        .filter(collections__slug="core-essentials")
        .order_by("-published_at")[:4]
    )

    context = {
        "seo_title": f"{site.site_name} - {site.tagline}",
        "seo_description": site.default_seo_description,
        "is_development": settings.DEBUG,
        "nav_categories": selectors.storefront_categories(limit=6, with_children=False),
        "featured_products": selectors.homepage_featured(limit=4),
        "new_arrivals": selectors.homepage_new_arrivals(limit=8),
        "essentials": essentials,
        "continue_shopping": continue_shopping,
        "recently_viewed": recently_viewed,
        "featured_collections": list(selectors.live_collections(featured_only=True)[:3]),
        "active_drops": active_drops,
        "loop_highlights": loop_highlights,
        # Phase 20: who runs this site and how to search it, from the real config row.
        "organization_schema": organization_schema(request),
        "website_schema": website_schema(request),
    }
    return render(request, "pages/home.html", context)


@require_GET
def robots(request: HttpRequest) -> HttpResponse:
    """``/robots.txt`` -- crawl rules and the sitemap pointer (Phase 20).

    Disallow is for private or machine-only surfaces where a crawler has nothing to index
    (and everything customer-facing stays open); the sitemap is absolute so it survives any
    proxy in front of the app.
    """
    lines = [
        "User-Agent: *",
        "Allow: /",
        "Disallow: /operations/",
        "Disallow: /admin/",
        "Disallow: /account/",
        "Disallow: /cart/",
        "Disallow: /shop/checkout",
        "Disallow: /api/",
        "",
        f"Sitemap: {request.build_absolute_uri(reverse('core:sitemap'))}",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def page_not_found(request: HttpRequest, exception) -> HttpResponse:
    """404 handler that uses the global error template."""
    return render(request, "pages/errors/404.html", {"exception": exception}, status=404)


def server_error(request: HttpRequest) -> HttpResponse:
    """500 handler that uses the global error template."""
    return render(request, "pages/errors/500.html", status=500)


def bad_request(request: HttpRequest, exception=None) -> HttpResponse:
    """400 handler (malformed requests, disallowed hosts, oversized payloads).

    Rendered *without* the request: the common cause of a 400 is an unusable
    ``Host`` header, and passing the request into template rendering would raise
    ``DisallowedHost`` a second time while building the error page itself.
    """
    from django.template.loader import render_to_string

    return HttpResponse(
        render_to_string("pages/errors/400.html", {"exception": exception}), status=400
    )


def permission_denied(request: HttpRequest, exception=None) -> HttpResponse:
    """403 handler (denied permissions) using the global template."""
    return render(request, "pages/errors/403.html", {"exception": exception}, status=403)


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Custom CSRF failure page (``settings.CSRF_FAILURE_VIEW``).

    Django routes CSRF rejections straight to this view instead of
    ``handler403``, so without it a failed CSRF check would show Django's
    technical page. The reason is already logged by the middleware; the page
    stays generic on purpose.
    """
    return render(request, "pages/errors/403.html", {"reason": reason}, status=403)
