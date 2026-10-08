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
    from apps.catalog.fashion_discovery import get_mood_catalog, get_occasion_catalog

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
    recently_viewed_raw = merchandising.get_recently_viewed(request, limit=6)
    cs_pks = {p.pk for p in continue_shopping}
    recently_viewed = [p for p in recently_viewed_raw if p.pk not in cs_pks][:4]

    # Returning Customer Personalization (Isolated to authenticated shoppers
    # to protect query budget)
    wishlist_items = []
    personalized_recommendations = []
    closet_pairing = None
    dna_style_goal = None

    if user and user.is_authenticated:
        # 1. Wishlist Items
        try:
            from apps.shop.models import WishlistItem

            wish_seen = cs_pks | {p.pk for p in recently_viewed}
            wish_qs = (
                WishlistItem.objects.filter(wishlist__user=user)
                .select_related("product", "product__brand")
                .prefetch_related("product__images", "product__variants")
                .order_by("-created_at")
            )
            if wish_seen:
                wish_qs = wish_qs.exclude(product_id__in=wish_seen)
            wishlist_items = [
                item.product
                for item in wish_qs[:4]
                if item.product and getattr(item.product, "status", None) == "active"
            ]
        except Exception:
            wishlist_items = []

        # 2. Personalized Recommendations based on FLASH DNA, closet, or orders
        try:
            from apps.recommendations.services import get_recommendations

            has_dna = hasattr(user, "flash_dna")
            has_closet = (
                hasattr(user, "closet_items") and user.closet_items.filter(status="active").exists()
            )
            has_orders = hasattr(user, "orders") and user.orders.exists()

            if has_dna or has_closet or has_orders:
                rec_res = get_recommendations(user, context="personalized", max_results=4)
                seen_all = cs_pks | {p.pk for p in recently_viewed} | {p.pk for p in wishlist_items}
                for rec in rec_res.get("recommendations", []):
                    prod = rec.get("product")
                    if (
                        prod
                        and prod.pk not in seen_all
                        and getattr(prod, "status", None) == "active"
                    ):
                        personalized_recommendations.append(rec)
                        seen_all.add(prod.pk)
                    if len(personalized_recommendations) >= 4:
                        break
        except Exception:
            personalized_recommendations = []

        # 3. DNA Style Goal / Direction
        try:
            if hasattr(user, "flash_dna") and user.flash_dna:
                dna_style_goal = (
                    getattr(user.flash_dna, "style_goal", None)
                    or getattr(user.flash_dna, "fashion_goal", None)
                    or getattr(user.flash_dna, "styles", None)
                )
        except Exception:
            dna_style_goal = None

        # 4. Closet Pairing (Complete the look with user's own wardrobe)
        try:
            if hasattr(user, "closet_items") and user.closet_items.filter(status="active").exists():
                lead_piece = None
                if continue_shopping:
                    lead_piece = continue_shopping[0]
                elif personalized_recommendations:
                    lead_piece = personalized_recommendations[0].get("product")
                elif wishlist_items:
                    lead_piece = wishlist_items[0]

                if lead_piece:
                    from apps.closet.services.matching import get_closet_matches_for_product

                    matches = get_closet_matches_for_product(user, lead_piece, limit=2)
                    if matches:
                        closet_pairing = {
                            "product": lead_piece,
                            "closet_item": matches[0],
                            "closet_items": matches,
                        }
        except Exception:
            closet_pairing = None

    is_returning_customer = bool(
        continue_shopping
        or recently_viewed
        or wishlist_items
        or personalized_recommendations
        or closet_pairing
    )

    essentials = list(
        selectors.storefront_products()
        .filter(collections__slug="core-essentials")
        .order_by("-published_at")[:4]
    )

    from apps.core.services.content import get_active_homepage_sections

    is_preview = bool(is_team_member and request.GET.get("preview") == "content")
    managed_sections = get_active_homepage_sections(include_unpublished=is_preview)

    managed_hero = None
    managed_banners = []
    managed_curated_rails = []
    for s in managed_sections:
        if s.section_type == "hero" and managed_hero is None:
            managed_hero = s
        elif s.section_type == "banner":
            managed_banners.append(s)
        else:
            managed_curated_rails.append(s)

    creator_community_looks = []
    try:
        from apps.creator.selectors import get_featured_posts

        creator_community_looks = get_featured_posts(limit=4)
    except Exception:
        pass

    # Track homepage_section_view for live store visits (not staff previews)
    if not is_preview and managed_sections:
        from apps.analytics.services import record_event

        for sec in managed_sections:
            record_event(
                name="homepage_section_view",
                request=request,
                metadata={
                    "section_id": str(sec.pk),
                    "section_type": sec.section_type,
                    "title": sec.title,
                },
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
        "wishlist_items": wishlist_items,
        "personalized_recommendations": personalized_recommendations,
        "closet_pairing": closet_pairing,
        "dna_style_goal": dna_style_goal,
        "is_returning_customer": is_returning_customer,
        "featured_collections": list(selectors.live_collections(featured_only=True)[:3]),
        "active_drops": active_drops,
        "loop_highlights": loop_highlights,
        # Content Studio (Phase 29 & 38)
        "managed_sections": managed_sections,
        "managed_hero": managed_hero,
        "managed_banners": managed_banners,
        "managed_curated_rails": managed_curated_rails,
        "is_content_preview": is_preview,
        "creator_community_looks": creator_community_looks,
        # Fashion Discovery 2.0 (Phase 34)
        "discovery_moods": get_mood_catalog(),
        "discovery_occasions": get_occasion_catalog(),
        # Phase 20: who runs this site and how to search it, from the real config row.
        "organization_schema": organization_schema(request),
        "website_schema": website_schema(request),
    }
    return render(request, "pages/home.html", context)


def track_content_interaction(request: HttpRequest) -> HttpResponse:
    """Track content and merchandising interactions (views, clicks)."""
    import json

    from django.shortcuts import redirect

    data: dict = {}
    if request.method == "POST":
        if request.content_type == "application/json" and request.body:
            try:
                data = json.loads(request.body)
            except Exception:
                data = {}
        else:
            data = request.POST.dict()
    elif request.method == "GET":
        data = request.GET.dict()
    else:
        return JsonResponse({"error": "Method not allowed"}, status=405)

    event_name = data.get("event")
    allowed_events = {
        "homepage_section_view",
        "homepage_section_click",
        "editorial_view",
        "editorial_click",
        "campaign_click",
        "featured_product_click",
    }
    if event_name in allowed_events:
        from apps.analytics.services import record_event
        from apps.core.services.content import validate_cta_destination

        metadata = {
            "section_id": data.get("section_id"),
            "section_type": data.get("section_type"),
            "section_title": data.get("section_title"),
            "story_id": data.get("story_id"),
            "story_title": data.get("story_title"),
            "campaign_id": data.get("campaign_id"),
            "campaign_title": data.get("campaign_title"),
            "product_id": data.get("product_id"),
            "destination": data.get("destination"),
        }
        record_event(name=event_name, request=request, metadata=metadata)

        destination = data.get("destination")
        if request.method == "GET" and destination:
            try:
                validate_cta_destination(destination)
                return redirect(destination)
            except Exception:
                return redirect("core:home")

        return JsonResponse({"status": "recorded"})

    return JsonResponse({"status": "ignored"}, status=400)


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
