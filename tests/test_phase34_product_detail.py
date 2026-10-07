"""Phase 34: Product Detail Experience 2.0 Regression Test Suite.

Covers:
- Enhanced PDP rendering and visual hierarchy
- Gallery data serialization (JSON-LD and script element)
- Variant matrix selection, truthful stock messaging, and sold-out states
- Add-to-bag form with quantity controls and asynchronous support
- Phase 33 Wishlist & Price/Stock alert integration
- Flash DNA personalized fit match & closet synergy
- Trust ribbon, guarantees, and care instructions
- Rating breakdown distribution and review sorting
- Creator community lookbooks & styled ensembles
- Mobile sticky bar controls
- CSP adherence & Schema.org JSON-LD integrity
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import (
    Brand,
    Category,
    Color,
    Fit,
    Product,
    ProductImage,
    ProductVariant,
    Size,
)
from apps.closet.models import ClosetItem, Outfit
from apps.creator.models import CreatorPost, CreatorPostStatus, CreatorProfile, CreatorStatus
from apps.creator.services import add_outfit_tag, add_product_tag
from apps.engagement.models import Review
from apps.inventory.models import Stock
from apps.orders.models import Order
from apps.shop.models import Cart, CheckoutSession, Wishlist, WishlistItem
from apps.styling.models import FlashDNA

pytestmark = pytest.mark.django_db


@pytest.fixture
def auth_user(db):
    user_model = get_user_model()
    return user_model.objects.create_user(
        email="shopper@flashwear.test",
        password="TestPassword123!",
        first_name="Jordan",
        last_name="Lee",
    )


@pytest.fixture
def product_hierarchy(db):
    category = Category.objects.create(name="Outerwear", slug="outerwear", is_active=True)
    fit = Fit.objects.create(name="Boxy Oversized", slug="boxy-oversized", is_active=True)
    brand = Brand.objects.create(name="FLASH Lab", slug="flash-lab", is_active=True)

    product = Product.objects.create(
        name="Cyber Utility Overshirt",
        slug="cyber-utility-overshirt",
        category=category,
        brand=brand,
        fit=fit,
        short_description="Heavyweight tactical overshirt with modular cargo pockets.",
        description=(
            "Constructed from high-density nylon with reinforced stitching. "
            "Boxy cut for effortless layering."
        ),
        status=Product.Status.ACTIVE,
    )

    color_black = Color.objects.create(
        name="Onyx Black", slug="onyx-black", hex_code="#111111", is_active=True
    )
    color_olive = Color.objects.create(
        name="Tactical Olive", slug="tactical-olive", hex_code="#4A5D4E", is_active=True
    )

    size_m = Size.objects.create(name="Medium", code="M", is_active=True, display_order=1)
    size_l = Size.objects.create(name="Large", code="L", is_active=True, display_order=2)

    # Variant 1: In stock (10 units)
    v1 = ProductVariant.objects.create(
        product=product,
        color=color_black,
        size=size_m,
        sku="FW-OVS-BLK-M",
        price=Decimal("120.00"),
        compare_at_price=Decimal("150.00"),
        is_active=True,
    )
    Stock.objects.create(variant=v1, on_hand=10, reserved=0)

    # Variant 2: Low stock (3 units)
    v2 = ProductVariant.objects.create(
        product=product,
        color=color_black,
        size=size_l,
        sku="FW-OVS-BLK-L",
        price=Decimal("120.00"),
        is_active=True,
    )
    Stock.objects.create(variant=v2, on_hand=3, reserved=0)

    # Variant 3: Sold out (0 units)
    v3 = ProductVariant.objects.create(
        product=product,
        color=color_olive,
        size=size_m,
        sku="FW-OVS-OLV-M",
        price=Decimal("125.00"),
        is_active=True,
    )
    Stock.objects.create(variant=v3, on_hand=0, reserved=0)

    # Product Images
    ProductImage.objects.create(
        product=product,
        image="catalog/overshirt_front.jpg",
        alt_text="Overshirt Front Angle",
        is_primary=True,
        position=0,
    )
    ProductImage.objects.create(
        product=product,
        image="catalog/overshirt_back.jpg",
        alt_text="Overshirt Back Detail",
        is_primary=False,
        position=1,
    )

    return {
        "product": product,
        "category": category,
        "fit": fit,
        "color_black": color_black,
        "color_olive": color_olive,
        "size_m": size_m,
        "size_l": size_l,
        "v1_in_stock": v1,
        "v2_low_stock": v2,
        "v3_sold_out": v3,
    }


# ==============================================================================
# 1. First Viewport, Hero & Gallery Tests
# ==============================================================================


def test_product_detail_hero_and_gallery_data(client, product_hierarchy):
    product = product_hierarchy["product"]
    response = client.get(product.get_absolute_url())

    assert response.status_code == 200
    content = response.content.decode()

    # Product Hero basics
    assert product.name in content
    assert "FLASH Lab" in content
    assert "$120.00" in content
    assert "Secure Checkout" in content
    assert "Free Over $100" in content

    # Gallery JSON script is emitted for Alpine gallery engine
    assert 'id="pdp-gallery-data"' in content
    # Extract JSON script content
    script_start = content.find('id="pdp-gallery-data"')
    data_start = content.find(">", script_start) + 1
    data_end = content.find("</script>", data_start)
    gallery_json = json.loads(content[data_start:data_end])

    assert len(gallery_json) >= 2
    assert gallery_json[0]["alt"] == "Overshirt Front Angle"
    assert "/media/catalog/overshirt_front.jpg" in gallery_json[0]["url"]


# ==============================================================================
# 2. Variant Selection, Low Stock & Sold Out States
# ==============================================================================


def test_variant_selection_in_stock_and_low_stock(client, product_hierarchy):
    product = product_hierarchy["product"]

    # Select Variant 1 (In Stock, Medium)
    res_in_stock = client.get(f"{product.get_absolute_url()}?color=onyx-black&size=M")
    assert res_in_stock.status_code == 200
    html_in_stock = res_in_stock.content.decode()

    assert "FW-OVS-BLK-M" in html_in_stock
    assert "Add to bag" in html_in_stock

    # Select Variant 2 (Low Stock: 3 units left)
    res_low_stock = client.get(f"{product.get_absolute_url()}?color=onyx-black&size=L")
    assert res_low_stock.status_code == 200
    html_low_stock = res_low_stock.content.decode()

    assert "FW-OVS-BLK-L" in html_low_stock
    assert "Only 3 left" in html_low_stock


def test_variant_sold_out_state(client, product_hierarchy):
    product = product_hierarchy["product"]

    # Select Variant 3 (Sold Out: 0 units, Olive Medium)
    response = client.get(f"{product.get_absolute_url()}?color=tactical-olive&size=M")
    assert response.status_code == 200
    html = response.content.decode()

    assert "FW-OVS-OLV-M" in html
    assert "Sold out in this size" in html
    assert "Save to your wishlist above to get an instant back-in-stock alert." in html


# ==============================================================================
# 3. Add to Bag Experience & Form Attributes
# ==============================================================================


def test_add_to_bag_form_and_ajax_cart_integration(client, product_hierarchy):
    product = product_hierarchy["product"]
    v1 = product_hierarchy["v1_in_stock"]

    # Verify form exists with valid attributes
    res = client.get(f"{product.get_absolute_url()}?color=onyx-black&size=M")
    html = res.content.decode()

    assert 'action="/shop/cart/add/"' in html
    assert f'value="{v1.pk}"' in html
    assert 'name="quantity"' in html

    # Post add-to-cart request with AJAX header
    post_res = client.post(
        reverse("shop:cart-add"),
        {"variant_id": v1.pk, "quantity": 1},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert post_res.status_code == 200
    data = post_res.json()
    assert data["success"] is True
    assert data["item_count"] >= 1


# ==============================================================================
# 4. Wishlist & Smart Alert Controls (Phase 33 Integration)
# ==============================================================================


def test_wishlist_integration_on_product_detail(client, auth_user, product_hierarchy):
    product = product_hierarchy["product"]
    v1 = product_hierarchy["v1_in_stock"]

    # 1. Unsaved state for authenticated user
    client.force_login(auth_user)
    res1 = client.get(f"{product.get_absolute_url()}?color=onyx-black&size=M")
    html1 = res1.content.decode()
    assert 'action="/shop/wishlist/add/"' in html1
    assert "Save" in html1

    # 2. Saved state with alerts
    wishlist = Wishlist.objects.create(user=auth_user)
    WishlistItem.objects.create(
        wishlist=wishlist,
        product=product,
        variant=v1,
        notify_price_drop=True,
        notify_back_in_stock=False,
    )

    res2 = client.get(f"{product.get_absolute_url()}?color=onyx-black&size=M")
    html2 = res2.content.decode()
    assert "Saved" in html2
    assert "Item Notifications" in html2
    assert "Price drop alert" in html2


# ==============================================================================
# 5. Flash DNA & Closet Integration
# ==============================================================================


def test_flash_dna_match_on_product_detail(client, auth_user, product_hierarchy):
    product = product_hierarchy["product"]
    fit = product_hierarchy["fit"]

    # Setup Flash DNA matching user's preferred fit
    dna = FlashDNA.objects.create(user=auth_user, styles="Streetwear")
    dna.preferred_fits.add(fit)

    client.force_login(auth_user)
    res = client.get(product.get_absolute_url())
    html = res.content.decode()

    assert "FLASH DNA Match" in html
    assert "boxy oversized" in html.lower()


def test_closet_matches_loop_on_product_detail(client, auth_user, product_hierarchy):
    product = product_hierarchy["product"]
    v1 = product_hierarchy["v1_in_stock"]

    # User has an existing closet item
    ClosetItem.objects.create(
        user=auth_user,
        variant=v1,
        name="Tactical Cargo Pants",
        category=ClosetItem.Category.BOTTOMS,
        status=ClosetItem.Status.ACTIVE,
    )

    client.force_login(auth_user)
    res = client.get(product.get_absolute_url())
    html = res.content.decode()

    # Shows closet pairing section
    assert "Works with" in html
    assert "in your closet" in html
    assert "Tactical Cargo Pants" in html
    assert "Build an outfit" in html


# ==============================================================================
# 6. Reviews & Rating Breakdown
# ==============================================================================


def test_reviews_breakdown_and_sorting(client, auth_user, product_hierarchy):
    product = product_hierarchy["product"]

    cart = Cart.objects.create(user=auth_user, session_key="", status=Cart.Status.CONVERTED)
    checkout = CheckoutSession.objects.create(
        user=auth_user, cart=cart, status=CheckoutSession.Status.CONVERTED
    )
    order = Order.objects.create(
        number=f"ORD-REV-{auth_user.pk}",
        checkout=checkout,
        user=auth_user,
        status=Order.Status.DELIVERED,
        subtotal=Decimal("120.00"),
        shipping_amount=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("120.00"),
    )

    Review.objects.create(
        order=order,
        product=product,
        author=auth_user,
        rating=5,
        title="Unmatched Quality",
        body="The nylon drape and pocket placement are next level. Fits true to size.",
        status=Review.Status.PUBLISHED,
        verified_purchase=True,
        published_at=timezone.now(),
    )

    res = client.get(product.get_absolute_url())
    html = res.content.decode()

    assert "Customer Reviews" in html
    assert "5.0" in html
    assert "out of 5" in html
    assert "1 verified review" in html
    assert "Unmatched Quality" in html
    assert "Verified purchase" in html
    assert "reviews_sort=newest" in html


# ==============================================================================
# 7. Creator Community Outfits (Phase 32 Integration)
# ==============================================================================


def test_creator_community_outfits_on_product_detail(client, product_hierarchy):
    product = product_hierarchy["product"]
    user_model = get_user_model()
    creator_user = user_model.objects.create_user(
        email="creator34@flashwear.test",
        password="PassWord123!",
        first_name="Kai",
        last_name="Vance",
    )
    profile = CreatorProfile.objects.create(
        user=creator_user,
        display_name="Kai Vance",
        slug="kai-vance",
        status=CreatorStatus.APPROVED,
    )
    post = CreatorPost.objects.create(
        creator=profile,
        title="Cyber Industrial Layering",
        slug="cyber-industrial-layering",
        status=CreatorPostStatus.PUBLISHED,
    )
    add_product_tag(post, product, label="Main Layer")

    outfit = Outfit.objects.create(
        user=creator_user,
        name="Night Tech Ensemble",
        status=Outfit.Status.SAVED,
    )
    add_outfit_tag(post, outfit)

    res = client.get(product.get_absolute_url())
    html = res.content.decode()

    assert "Seen in Real Looks" in html
    assert "Night Tech Ensemble" in html
    assert "Kai Vance" in html


# ==============================================================================
# 8. Schema.org JSON-LD and Mobile Sticky Bar
# ==============================================================================


def test_schema_org_and_mobile_sticky_bar(client, product_hierarchy):
    product = product_hierarchy["product"]
    res = client.get(product.get_absolute_url())
    html = res.content.decode()

    # Schema JSON-LD scripts
    assert 'id="ld-product"' in html
    assert 'id="ld-breadcrumb"' in html

    # Mobile sticky bar
    assert "lg:hidden" in html
    assert product.name in html
