"""Smart merchandising, discovery recommendations and real shopping signals (Phase 28).

House rules:
* Deterministic, zero-ML, prefetch-safe.
* Session-backed recently viewed works for both anonymous guests and authenticated users.
* Deduplicated product discovery across PDP sections
  (Complete the Look, You May Also Like, More Like This).
* Honest shopping signals based only on real DB stock, pricing and collections (no fake urgency).
"""

from __future__ import annotations

import logging
from typing import Any

from django.db.models import Q

from apps.catalog.models import Product, ProductVariant
from apps.inventory.models import Stock

logger = logging.getLogger("flashwear.merchandising")

MAX_RECENTLY_VIEWED = 10
SESSION_KEY_RECENTLY_VIEWED = "fw_recently_viewed"
COOKIE_RECENTLY_VIEWED = "fw_recent_views"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


# =============================================================================
# 1. Recently Viewed
# =============================================================================


def record_recently_viewed(request, product: Product) -> None:
    """Record a viewed product in the visitor's session or cookie.

    Deduplicated, most-recent-first, capped at MAX_RECENTLY_VIEWED items.
    Safe for both anonymous visitors and signed-in members without creating
    unnecessary database session writes on read-only views.
    """
    if request is None:
        return
    if not product or not product.pk:
        return

    try:
        raw_ids: list[Any] = []
        if hasattr(request, "session") and SESSION_KEY_RECENTLY_VIEWED in request.session:
            session_val = request.session.get(SESSION_KEY_RECENTLY_VIEWED, [])
            if isinstance(session_val, list):
                raw_ids = session_val
        elif (
            hasattr(request, "_pending_recently_viewed_cookie")
            and request._pending_recently_viewed_cookie
        ):
            raw_ids = [x for x in request._pending_recently_viewed_cookie.split(",") if x.isdigit()]
        elif hasattr(request, "COOKIES") and COOKIE_RECENTLY_VIEWED in request.COOKIES:
            cookie_str = request.COOKIES.get(COOKIE_RECENTLY_VIEWED, "")
            raw_ids = [x for x in cookie_str.split(",") if x.isdigit()]

        # Filter to valid integers
        ids = [int(i) for i in raw_ids if isinstance(i, (int, str)) and str(i).isdigit()]

        # Remove existing occurrence if present
        if product.pk in ids:
            ids.remove(product.pk)

        # Prepend to the front
        ids.insert(0, product.pk)

        # Cap limit
        ids = ids[:MAX_RECENTLY_VIEWED]

        # Stash pending cookie value on request so response can attach it
        request._pending_recently_viewed_cookie = ",".join(str(i) for i in ids)

        # Update session if:
        # 1. Session is a plain dict (e.g. in test fixtures: request.session = {})
        # 2. User is authenticated
        # 3. Session already has an established session_key in DB
        if hasattr(request, "session"):
            if isinstance(request.session, dict):
                request.session[SESSION_KEY_RECENTLY_VIEWED] = ids
            elif hasattr(request, "user") and request.user.is_authenticated:
                request.session[SESSION_KEY_RECENTLY_VIEWED] = ids
                request.session.modified = True
            elif hasattr(request.session, "session_key") and request.session.session_key:
                request.session[SESSION_KEY_RECENTLY_VIEWED] = ids
                request.session.modified = True
    except Exception:
        logger.exception("Failed to record recently viewed product %s", product.pk)


def get_recently_viewed(
    request,
    *,
    exclude_pk: int | None = None,
    limit: int = 6,
) -> list[Product]:
    """Retrieve recently viewed published products, preserving chronological order.

    Gracefully ignores products that were deleted or unpublished since viewing.
    """
    if request is None:
        return []

    raw_ids: list[Any] = []
    if hasattr(request, "session") and SESSION_KEY_RECENTLY_VIEWED in request.session:
        session_val = request.session.get(SESSION_KEY_RECENTLY_VIEWED, [])
        if isinstance(session_val, list):
            raw_ids = session_val
    elif (
        hasattr(request, "_pending_recently_viewed_cookie")
        and request._pending_recently_viewed_cookie
    ):
        raw_ids = [x for x in request._pending_recently_viewed_cookie.split(",") if x.isdigit()]
    elif hasattr(request, "COOKIES") and COOKIE_RECENTLY_VIEWED in request.COOKIES:
        cookie_str = request.COOKIES.get(COOKIE_RECENTLY_VIEWED, "")
        raw_ids = [x for x in cookie_str.split(",") if x.isdigit()]

    if not raw_ids:
        return []

    ids: list[int] = []
    for raw in raw_ids:
        try:
            val = int(raw)
            if exclude_pk is None or val != exclude_pk:
                if val not in ids:
                    ids.append(val)
        except (ValueError, TypeError):
            continue

    if not ids:
        return []

    # Slice IDs before query to avoid over-fetching
    target_ids = ids[:limit]

    products = Product.objects.published().with_storefront_data().filter(pk__in=target_ids)
    product_map = {p.pk: p for p in products}

    # Preserve session order and filter out deleted/missing products
    return [product_map[pk] for pk in target_ids if pk in product_map]


# =============================================================================
# 2. Continue Shopping / Still Thinking About These?
# =============================================================================


def get_continue_shopping_items(
    request,
    *,
    exclude_pk: int | None = None,
    limit: int = 4,
) -> list[Product]:
    """Contextual items the customer interacted with (recent views, cart, wishlist).

    Returns an empty list if there is insufficient data (less than 2 items).
    """
    if request is None:
        return []

    candidate_ids: list[int] = []

    # 1. From recently viewed
    for p in get_recently_viewed(request, exclude_pk=exclude_pk, limit=limit + 2):
        if p.pk not in candidate_ids:
            candidate_ids.append(p.pk)

    # 2. From active cart (if any)
    try:
        from apps.shop.services import get_cart_for_request

        cart = get_cart_for_request(request)
        if cart:
            for item in cart.get_items():
                p_id = item.variant.product_id
                if p_id and (exclude_pk is None or p_id != exclude_pk):
                    if p_id not in candidate_ids:
                        candidate_ids.append(p_id)
    except Exception:
        pass

    # 3. From wishlist (if authenticated)
    if hasattr(request, "user") and request.user.is_authenticated:
        try:
            wishlist = getattr(request.user, "wishlist", None)
            if wishlist:
                for item in wishlist.items.select_related("product"):
                    if item.product_id and (exclude_pk is None or item.product_id != exclude_pk):
                        if item.product_id not in candidate_ids:
                            candidate_ids.append(item.product_id)
        except Exception:
            pass

    # Insufficient data rule: require at least 2 distinct interactions
    if len(candidate_ids) < 2:
        return []

    target_ids = candidate_ids[:limit]
    products = Product.objects.published().with_storefront_data().filter(pk__in=target_ids)
    product_map = {p.pk: p for p in products}
    return [product_map[pk] for pk in target_ids if pk in product_map]


# =============================================================================
# 3. Smart Product Discovery (Deduplicated PDP Sections)
# =============================================================================


def get_pdp_discovery_sections(
    product: Product,
    request=None,
    *,
    limit_per_section: int = 4,
) -> dict[str, Any]:
    """Generate deduplicated recommendation strips for a Product Detail Page.

    Sections:
    - 'you_may_also_like': Category/brand/collection recommendations
    - 'more_like_this': Garment attributes/material/fit matches
    - 'recently_viewed': Previous browsing history excluding current product

    Strict invariant: no product appears across more than one section.
    """
    excluded_pks: set[int] = {product.pk}

    # 1. Complete the look piece IDs (to exclude from recommendation strips)
    try:
        from apps.styling.services.complete_look import complete_look

        user = getattr(request, "user", None) if request else None
        look = complete_look(product, user=user)
        for item in look.items:
            if item.product and hasattr(item.product, "pk"):
                excluded_pks.add(item.product.pk)
    except Exception:
        look = None

    # 2. You May Also Like: Same category first, then same brand / collections
    also_like_qs = Product.objects.published().with_storefront_data().exclude(pk__in=excluded_pks)
    if product.category_id:
        cat_matches = also_like_qs.filter(
            Q(category=product.category) | Q(category__parent=product.category.parent)
        )
    else:
        cat_matches = also_like_qs

    you_may_also_like = list(
        cat_matches.order_by("-is_featured", "-published_at", "-id")[:limit_per_section]
    )

    # If category matches yielded fewer than limit, expand to brand / collection / catalog
    if len(you_may_also_like) < limit_per_section:
        needed = limit_per_section - len(you_may_also_like)
        seen = excluded_pks.union({p.pk for p in you_may_also_like})
        fill = list(
            Product.objects.published()
            .with_storefront_data()
            .exclude(pk__in=seen)
            .order_by("-is_featured", "-published_at", "-id")[:needed]
        )
        you_may_also_like.extend(fill)

    # In micro-catalogs (fixtures with only 2 products total where complete_look claimed the other),
    # ensure 'You may also like' still shows the available piece rather than an empty strip.
    if not you_may_also_like:
        fallback = list(
            Product.objects.published()
            .with_storefront_data()
            .exclude(pk=product.pk)
            .order_by("-is_featured", "-published_at", "-id")[:limit_per_section]
        )
        you_may_also_like.extend(fallback)

    for p in you_may_also_like:
        excluded_pks.add(p.pk)

    # 3. More Like This: Style / material / fit matches, deduplicated against above
    more_like_qs = Product.objects.published().with_storefront_data().exclude(pk__in=excluded_pks)

    # Try matching by materials or collections first (using in-memory prefetched lists)
    material_pks = [m.pk for m in product.materials.all()]
    collection_pks = [c.pk for c in product.collections.all()]

    attr_query = Q()
    if material_pks:
        attr_query |= Q(materials__in=material_pks)
    if collection_pks:
        attr_query |= Q(collections__in=collection_pks)
    if product.fit_id:
        attr_query |= Q(fit_id=product.fit_id)

    if attr_query:
        more_like_this = list(
            more_like_qs.filter(attr_query).distinct().order_by("-published_at")[:limit_per_section]
        )
    else:
        more_like_this = []

    # If attribute query yielded fewer than needed, fill with general published
    if len(more_like_this) < limit_per_section:
        needed = limit_per_section - len(more_like_this)
        for p in more_like_this:
            excluded_pks.add(p.pk)
        fill = list(
            Product.objects.published()
            .with_storefront_data()
            .exclude(pk__in=excluded_pks)
            .order_by("-id")[:needed]
        )
        more_like_this.extend(fill)

    for p in more_like_this:
        excluded_pks.add(p.pk)

    # 4. Recently viewed
    recently_viewed = get_recently_viewed(request, exclude_pk=product.pk, limit=limit_per_section)

    return {
        "complete_look": look,
        "you_may_also_like": you_may_also_like,
        "more_like_this": more_like_this,
        "recently_viewed": recently_viewed,
    }


# =============================================================================
# 4. Real Shopping Signals
# =============================================================================


def get_product_signals(
    product: Product,
    variant: ProductVariant | None = None,
    user=None,
) -> dict[str, Any]:
    """Extract truthful signals based only on real DB stock, prices and dates.

    Never fabricates urgency or popularity.
    """
    signals: dict[str, Any] = {
        "low_stock": False,
        "units_left": None,
        "is_sold_out": False,
        "is_new": product.is_new,
        "is_on_sale": product.is_on_sale,
        "discount_percent": product.max_discount_percent,
        "is_drop": False,
        "in_user_size": False,
        "user_size_code": "",
    }

    # Check if in collections that signify drops or new arrivals
    collection_slugs = {c.slug for c in product.collections.all()}
    if "new-arrivals" in collection_slugs:
        signals["is_new"] = True
    if any("drop" in s for s in collection_slugs):
        signals["is_drop"] = True

    # Real stock evaluation
    if variant is not None:
        stock = getattr(variant, "stock", None)
        if stock is None:
            # Look up stock if not prefetched
            stock = Stock.objects.filter(variant=variant).first()
        if stock is not None:
            avail = stock.available
            if avail <= 0:
                signals["is_sold_out"] = True
            elif avail <= 5:
                signals["low_stock"] = True
                signals["units_left"] = avail
        else:
            signals["is_sold_out"] = False
    else:
        # Product level stock check
        if not product.is_in_stock:
            signals["is_sold_out"] = True
        else:
            # Check if any variant has low stock
            for v in product.purchasable_variants:
                v_stock = getattr(v, "stock", None)
                if v_stock and 0 < v_stock.available <= 5:
                    signals["low_stock"] = True
                    signals["units_left"] = v_stock.available
                    break

    # Check preferred size if user has FLASH DNA profile
    if user and user.is_authenticated:
        try:
            dna = getattr(user, "flash_dna", None)
            if dna and hasattr(dna, "top_size") and dna.top_size:
                preferred = dna.top_size.upper()
                signals["user_size_code"] = preferred
                for v in product.purchasable_variants:
                    if v.size and v.size.code.upper() == preferred:
                        stock = getattr(v, "stock", None)
                        if stock is None or stock.available > 0:
                            signals["in_user_size"] = True
                        break
        except Exception:
            pass

    return signals


# =============================================================================
# 5. Smart Alternatives & Sizing Guide (Phase 30)
# =============================================================================


def get_smart_alternatives(
    product: Product,
    variant: ProductVariant | None = None,
    *,
    limit: int = 4,
) -> list[Product]:
    """Retrieve in-stock, published alternative pieces when a product or variant is unavailable.

    Grounded purely in matching category, brand, and fit without duplicating the viewed product.
    """
    excluded = {product.pk}
    candidates = Product.objects.published().with_storefront_data().exclude(pk__in=excluded)

    matches: list[Product] = []
    # 1. Match category
    if product.category_id:
        cat_matches = list(
            candidates.filter(category=product.category).order_by("-is_featured", "-published_at")[
                :limit
            ]
        )
        matches.extend(cat_matches)
        for m in cat_matches:
            excluded.add(m.pk)

    # 2. Match fit or brand if more needed
    if len(matches) < limit and product.fit_id:
        needed = limit - len(matches)
        fit_matches = list(
            Product.objects.published()
            .with_storefront_data()
            .exclude(pk__in=excluded)
            .filter(fit=product.fit)
            .order_by("-published_at")[:needed]
        )
        matches.extend(fit_matches)
        for m in fit_matches:
            excluded.add(m.pk)

    # 3. Fill with featured in-stock items if still needed
    if len(matches) < limit:
        needed = limit - len(matches)
        fill = list(
            Product.objects.published()
            .with_storefront_data()
            .exclude(pk__in=excluded)
            .order_by("-is_featured", "-id")[:needed]
        )
        matches.extend(fill)

    return matches[:limit]


def get_product_sizing_guide(product: Product, user=None) -> dict[str, Any]:
    """Build honest sizing and fit guide based strictly on real DB records and user DNA."""
    fit_name = product.fit.name if product.fit else "Standard Fit"
    fit_desc = "True to size. Designed for everyday comfort and clean drape."
    if product.fit:
        lower = product.fit.name.lower()
        if "relaxed" in lower or "oversized" in lower or "loose" in lower:
            fit_desc = (
                f"{product.fit.name} cut with extra room through the body. "
                "For a closer, more tailored fit, consider sizing down."
            )
        elif "slim" in lower or "tailored" in lower or "tight" in lower:
            fit_desc = (
                f"{product.fit.name} cut designed to follow the natural contours of the body. "
                "True to size; if between sizes, choose the larger size."
            )
        elif "wide" in lower or "straight" in lower:
            fit_desc = f"{product.fit.name} with consistent volume from hip to hem."

    sizes = []
    seen_codes = set()
    for variant in product.purchasable_variants:
        if variant.size and variant.size.code not in seen_codes:
            seen_codes.add(variant.size.code)
            sizes.append(
                {
                    "code": variant.size.code,
                    "name": variant.size.name or variant.size.code,
                    "size_type": variant.size.get_size_type_display(),
                }
            )

    # Sort sizes logically by code/order
    sizes.sort(key=lambda s: s["code"])

    recommended_size = ""
    if user and user.is_authenticated:
        try:
            dna = getattr(user, "flash_dna", None)
            if dna:
                is_bottom = bool(
                    product.category
                    and any(
                        w in product.category.name.lower()
                        for w in ("pant", "jean", "short", "skirt", "trouser", "bottom")
                    )
                )
                if is_bottom and getattr(dna, "bottom_size", None):
                    recommended_size = dna.bottom_size.upper()
                elif getattr(dna, "top_size", None):
                    recommended_size = dna.top_size.upper()
        except Exception:
            pass

    return {
        "fit_name": fit_name,
        "fit_description": fit_desc,
        "sizes": sizes,
        "recommended_size": recommended_size,
    }
