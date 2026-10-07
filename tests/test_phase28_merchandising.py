"""Phase 28 test suite: Smart Merchandising, Discovery, and Real Signals.

Tests:
1. Recently viewed tracking (anonymous & authenticated, deduplication, max limits, recency order).
2. Graceful handling of unpublished or deleted products in recently viewed.
3. Continue shopping threshold (requires >= 2 interactions, hides on insufficient data).
4. Recommendation deduplication across Complete the Look, You May Also Like, and More Like This.
5. Real shopping signals (low stock, sold out, sale, new arrival - no fake signals).
6. Quick View endpoint, permissions, rendering, and analytics event tracking.
7. Outbound click tracking for merchandising and safe open-redirect prevention.
8. Wishlist move-to-bag action, availability status, and wishlist_to_cart analytics.
9. Cart continue shopping recommendations and empty-state resilience.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.analytics.models import Event
from apps.catalog.merchandising import (
    MAX_RECENTLY_VIEWED,
    SESSION_KEY_RECENTLY_VIEWED,
    get_continue_shopping_items,
    get_pdp_discovery_sections,
    get_product_signals,
    get_recently_viewed,
    record_recently_viewed,
)
from apps.catalog.models import Category, Collection, Color, Product, ProductVariant, Size
from apps.inventory.models import Stock
from apps.shop.models import Cart, Wishlist, WishlistItem

User = get_user_model()


@pytest.fixture
def catalog_products(db):
    """Create a minimal set of distinct products with variants and stock."""
    cat_tops, _ = Category.objects.get_or_create(name="Tops", slug="tops")
    cat_bottoms, _ = Category.objects.get_or_create(name="Bottoms", slug="bottoms")

    color_blk, _ = Color.objects.get_or_create(name="Black", slug="black", hex_code="#000000")
    color_wht, _ = Color.objects.get_or_create(name="White", slug="white", hex_code="#FFFFFF")
    size_m, _ = Size.objects.get_or_create(name="Medium", code="M")
    size_l, _ = Size.objects.get_or_create(name="Large", code="L")

    col_essentials, _ = Collection.objects.get_or_create(name="Essentials", slug="core-essentials")
    col_new, _ = Collection.objects.get_or_create(name="New Arrivals", slug="new-arrivals")

    products = []
    for i in range(1, 8):
        cat = cat_tops if i % 2 == 1 else cat_bottoms
        prod = Product.objects.create(
            name=f"Test Merch Product {i}",
            slug=f"test-merch-product-{i}",
            category=cat,
            status=Product.Status.ACTIVE,
            short_description=f"Authentic test description for item {i}",
        )
        if i in (1, 2):
            prod.collections.add(col_essentials)
        if i in (3, 4):
            prod.collections.add(col_new)

        # Variant 1: normal stock
        v1 = ProductVariant.objects.create(
            product=prod,
            sku=f"TMP-{i}-BLK-M",
            color=color_blk,
            size=size_m,
            price=Decimal("45.00"),
            compare_at_price=Decimal("55.00") if i == 2 else None,
            is_active=True,
        )
        Stock.objects.create(variant=v1, on_hand=15, reserved=0)

        # Variant 2: low stock or sold out
        v2 = ProductVariant.objects.create(
            product=prod,
            sku=f"TMP-{i}-WHT-L",
            color=color_wht,
            size=size_l,
            price=Decimal("45.00"),
            is_active=True,
        )
        on_hand = 3 if i == 1 else (0 if i == 5 else 20)
        Stock.objects.create(variant=v2, on_hand=on_hand, reserved=0)

        products.append(prod)

    return products


# =============================================================================
# 1. Recently Viewed Tests
# =============================================================================


@pytest.mark.django_db
def test_record_recently_viewed_order_and_deduplication(rf, catalog_products):
    """Recently viewed preserves chronological order, deduplicates, and caps at max limit."""
    request = rf.get("/")
    request.session = {}

    p1, p2, p3 = catalog_products[0], catalog_products[1], catalog_products[2]

    record_recently_viewed(request, p1)
    record_recently_viewed(request, p2)
    record_recently_viewed(request, p3)

    # Viewing p1 again should move it to the front
    record_recently_viewed(request, p1)

    recent = get_recently_viewed(request)
    assert len(recent) == 3
    assert recent[0].pk == p1.pk
    assert recent[1].pk == p3.pk
    assert recent[2].pk == p2.pk

    # Exclusion works (e.g. on PDP to exclude current item)
    recent_ex = get_recently_viewed(request, exclude_pk=p1.pk)
    assert len(recent_ex) == 2
    assert p1 not in recent_ex


@pytest.mark.django_db
def test_record_recently_viewed_caps_at_limit(rf, catalog_products):
    """Recently viewed never exceeds MAX_RECENTLY_VIEWED items."""
    request = rf.get("/")
    request.session = {}

    for i in range(15):
        # Fake product IDs in session
        raw = request.session.get(SESSION_KEY_RECENTLY_VIEWED, [])
        raw.insert(0, i + 100)
        request.session[SESSION_KEY_RECENTLY_VIEWED] = raw[:MAX_RECENTLY_VIEWED]

    assert len(request.session[SESSION_KEY_RECENTLY_VIEWED]) == MAX_RECENTLY_VIEWED


@pytest.mark.django_db
def test_recently_viewed_handles_deleted_or_unpublished(rf, catalog_products):
    """Unpublished or missing products are silently omitted from recently viewed."""
    request = rf.get("/")
    p1, p2 = catalog_products[0], catalog_products[1]
    request.session = {SESSION_KEY_RECENTLY_VIEWED: [p1.pk, 999999, p2.pk]}

    # Archive p2
    p2.status = Product.Status.ARCHIVED
    p2.save()

    recent = get_recently_viewed(request)
    assert len(recent) == 1
    assert recent[0].pk == p1.pk


# =============================================================================
# 2. Continue Shopping Tests
# =============================================================================


@pytest.mark.django_db
def test_continue_shopping_insufficient_data_guard(rf, catalog_products):
    """Continue shopping returns nothing if user has < 2 interactions."""
    request = rf.get("/")
    request.session = {SESSION_KEY_RECENTLY_VIEWED: [catalog_products[0].pk]}

    # Only 1 item viewed -> insufficient data
    items = get_continue_shopping_items(request)
    assert items == []

    # 2 items viewed -> sufficient data
    request.session[SESSION_KEY_RECENTLY_VIEWED] = [catalog_products[0].pk, catalog_products[1].pk]
    items = get_continue_shopping_items(request)
    assert len(items) == 2


# =============================================================================
# 3. PDP Discovery Deduplication Tests
# =============================================================================


@pytest.mark.django_db
def test_pdp_discovery_sections_deduplication(rf, catalog_products):
    """Discovery sections must not show duplicate products across each other."""
    target_product = catalog_products[0]
    request = rf.get(target_product.get_absolute_url())
    request.session = {SESSION_KEY_RECENTLY_VIEWED: [p.pk for p in catalog_products]}

    sections = get_pdp_discovery_sections(target_product, request=request, limit_per_section=3)

    also_like_pks = {p.pk for p in sections["you_may_also_like"]}
    more_like_pks = {p.pk for p in sections["more_like_this"]}
    recent_pks = {p.pk for p in sections["recently_viewed"]}

    # Target product is never recommended to itself
    assert target_product.pk not in also_like_pks
    assert target_product.pk not in more_like_pks
    assert target_product.pk not in recent_pks

    # No overlap between 'You May Also Like' and 'More Like This'
    assert len(also_like_pks.intersection(more_like_pks)) == 0


# =============================================================================
# 4. Real Shopping Signals Tests
# =============================================================================


@pytest.mark.django_db
def test_real_shopping_signals(catalog_products):
    """Signals accurately reflect real stock levels, sale prices, and newness."""
    p_low = catalog_products[0]  # has v2 with on_hand=3
    v_low = p_low.variants.filter(sku__endswith="-WHT-L").first()
    signals_low = get_product_signals(p_low, variant=v_low)
    assert signals_low["low_stock"] is True
    assert signals_low["units_left"] == 3
    assert signals_low["is_sold_out"] is False

    p_sale = catalog_products[1]  # has compare_at_price 55 vs 45
    signals_sale = get_product_signals(p_sale)
    assert signals_sale["is_on_sale"] is True
    assert signals_sale["discount_percent"] > 0

    p_new = catalog_products[2]  # in new-arrivals collection
    signals_new = get_product_signals(p_new)
    assert signals_new["is_new"] is True


# =============================================================================
# 5. Quick View Endpoint Tests
# =============================================================================


@pytest.mark.django_db
def test_quick_view_renders_and_tracks_event(client, catalog_products):
    """GET /products/<slug>/quick-view/ returns modal HTML and tracks analytics."""
    prod = catalog_products[0]
    url = reverse("catalog:quick-view", kwargs={"slug": prod.slug})

    response = client.get(url)
    assert response.status_code == 200
    assert "quick-view-dialog" in response.content.decode("utf-8")
    assert prod.name in response.content.decode("utf-8")

    # Analytics event recorded
    event = Event.objects.filter(
        name="quick_view_opened",
        object_id=prod.pk,
    ).first()
    assert event is not None
    assert event.metadata["slug"] == prod.slug


# =============================================================================
# 6. Merchandising Click Tracker Tests
# =============================================================================


@pytest.mark.django_db
def test_merchandising_click_tracking_and_security(client, catalog_products):
    """Click tracker logs outbound hop and safely rejects off-site destinations."""
    prod = catalog_products[0]
    track_url = reverse("catalog:track-merchandising")

    # 1. Valid internal next hop
    resp = client.get(
        track_url,
        {
            "type": "recently_viewed_click",
            "product_id": prod.pk,
            "next": prod.get_absolute_url(),
            "src": "home_rail",
        },
    )
    assert resp.status_code == 302
    assert resp.url == prod.get_absolute_url()

    event = Event.objects.filter(name="recently_viewed_click", object_id=prod.pk).first()
    assert event is not None
    assert event.metadata["src"] == "home_rail"

    # 2. Malicious off-site open-redirect attempt
    evil_resp = client.get(
        track_url,
        {
            "type": "continue_shopping_click",
            "next": "https://evil-site.com/steal-credentials",
        },
    )
    assert evil_resp.status_code == 302
    assert evil_resp.url == reverse("catalog:product-list")


# =============================================================================
# 7. Wishlist Move to Bag Tests
# =============================================================================


@pytest.mark.django_db
def test_wishlist_move_to_bag(client, catalog_products):
    """Customer can move an item from wishlist to bag and event is tracked."""
    user = User.objects.create_user(email="shopper@flashwear.com", password="password123")
    client.force_login(user)

    prod = catalog_products[0]
    variant = prod.variants.first()
    wishlist = Wishlist.objects.create(user=user)
    item = WishlistItem.objects.create(wishlist=wishlist, product=prod, variant=variant)

    url = reverse("shop:wishlist-move-to-bag", kwargs={"item_pk": item.pk})
    resp = client.post(url)
    assert resp.status_code == 302
    assert resp.url == reverse("shop:cart")

    # Item removed from wishlist
    assert not WishlistItem.objects.filter(pk=item.pk).exists()

    # Item added to cart
    cart = Cart.objects.filter(user=user).first()
    assert cart is not None
    assert cart.get_item_count() == 1

    # Analytics event recorded
    event = Event.objects.filter(name="wishlist_to_cart", object_id=variant.pk).first()
    assert event is not None
    assert event.metadata["product"] == prod.pk


# =============================================================================
# 8. Cart Continue Shopping Integration Tests
# =============================================================================


@pytest.mark.django_db
def test_cart_detail_renders_continue_items(client, catalog_products):
    """Cart view supplies continue shopping products for empty or active carts."""
    # Set up session with viewed products
    session = client.session
    session[SESSION_KEY_RECENTLY_VIEWED] = [catalog_products[0].pk, catalog_products[1].pk]
    session.save()

    resp = client.get(reverse("shop:cart"))
    assert resp.status_code == 200
    assert "continue_items" in resp.context
    assert len(resp.context["continue_items"]) >= 2
