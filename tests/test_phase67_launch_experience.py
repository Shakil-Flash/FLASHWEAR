"""Phase 67: Launch Experience & Conversion Optimization Test Suite.

Verifies storefront polish, brand trust signals, variant selection UX,
dynamic currency handling, cart/mini-cart integrity, and ethical stock signaling.
"""

from decimal import Decimal
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from apps.catalog.models import Brand, Category, Color, Fit, Product, ProductVariant, Size
from apps.inventory.models import Stock
from apps.shop.models import Cart, CartItem
from apps.shop.services import get_or_create_cart, add_to_cart

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def store_catalog():
    """Setup a sample catalog with categories, colors, sizes, and products."""
    cat = Category.objects.create(name="Outerwear", slug="outerwear")
    brand = Brand.objects.create(name="FLASH Atelier", slug="flash-atelier")
    fit = Fit.objects.create(name="Oversized", slug="oversized")
    color_black = Color.objects.create(name="Midnight Black", slug="midnight-black", hex_code="#000000")
    color_taupe = Color.objects.create(name="Taupe Grey", slug="taupe-grey", hex_code="#8b8589")
    size_m = Size.objects.create(name="Medium", slug="m", code="M", display_order=2)
    size_l = Size.objects.create(name="Large", slug="l", code="L", display_order=3)

    product = Product.objects.create(
        name="Heavyweight Oversized Hoodie",
        slug="heavyweight-oversized-hoodie",
        category=cat,
        brand=brand,
        fit=fit,
        status="active",
        short_description="Garment-dyed 500gsm organic cotton hoodie.",
    )

    # Variant 1: Medium (Low stock: 3 units)
    v1 = ProductVariant.objects.create(
        product=product,
        sku="HOODIE-BLK-M",
        price=Decimal("85.00"),
        compare_at_price=Decimal("110.00"),
        is_active=True,
        color=color_black,
        size=size_m,
    )
    Stock.objects.create(variant=v1, on_hand=3, reserved=0)

    # Variant 2: Large (In stock: 20 units)
    v2 = ProductVariant.objects.create(
        product=product,
        sku="HOODIE-BLK-L",
        price=Decimal("85.00"),
        is_active=True,
        color=color_black,
        size=size_l,
    )
    Stock.objects.create(variant=v2, on_hand=20, reserved=0)

    # Product 2: Limited Edition Tee (All variants low stock <= 5)
    prod_limited = Product.objects.create(
        name="Limited Edition Studio Tee",
        slug="limited-edition-studio-tee",
        category=cat,
        brand=brand,
        fit=fit,
        status="active",
    )
    v3 = ProductVariant.objects.create(
        product=prod_limited,
        sku="TEE-TAUPE-M",
        price=Decimal("45.00"),
        is_active=True,
        color=color_taupe,
        size=size_m,
    )
    Stock.objects.create(variant=v3, on_hand=2, reserved=0)

    # Product 3: Bare product with no variants
    prod_bare = Product.objects.create(
        name="Upcoming Capsule Jacket",
        slug="upcoming-capsule-jacket",
        category=cat,
        brand=brand,
        fit=fit,
        status="active",
    )

    return {
        "category": cat,
        "brand": brand,
        "fit": fit,
        "product": product,
        "limited_product": prod_limited,
        "bare_product": prod_bare,
        "v1": v1,
        "v2": v2,
        "v3": v3,
        "color_black": color_black,
        "size_m": size_m,
        "size_l": size_l,
    }


def test_homepage_brand_trust_strip_renders(client: Client, store_catalog):
    """Homepage should render the 4-pillar brand trust & value proposition strip."""
    url = reverse("core:home")
    res = client.get(url)
    assert res.status_code == 200
    html = res.content.decode("utf-8")

    # Check 4 pillars
    assert "Complimentary Delivery" in html
    assert "30-Day Hassle-Free Returns" in html
    assert "FLASH Loop Circular Fashion" in html
    assert "Dedicated Support" in html
    assert "Brand Guarantees" in html


def test_product_detail_unselected_cta_and_trust(client: Client, store_catalog):
    """PDP should render dynamic trust threshold and provide guidance CTA when no variant is selected."""
    prod = store_catalog["product"]
    url = reverse("catalog:product-detail", kwargs={"slug": prod.slug})
    res = client.get(url)
    assert res.status_code == 200
    html = res.content.decode("utf-8")

    # Selected variant: add to bag & dynamic trust strip
    assert "Add to bag" in html
    assert "Free Over" in html
    assert "30-Day Return Window" in html

    # Bare product without selected variant offers guidance linking to options
    bare_prod = store_catalog["bare_product"]
    bare_res = client.get(reverse("catalog:product-detail", kwargs={"slug": bare_prod.slug}))
    assert bare_res.status_code == 200
    bare_html = bare_res.content.decode("utf-8")
    assert "Select a Size &amp; Colour Above" in bare_html or "Select a Size & Colour Above" in bare_html
    assert 'href="#variant-options"' in bare_html


def test_product_model_genuine_low_stock_property(store_catalog):
    """Product.is_low_stock should accurately reflect whether all available variants have <= 5 units."""
    normal_product = store_catalog["product"]
    limited_product = store_catalog["limited_product"]

    # normal_product has v2 with 20 units -> not low stock
    assert normal_product.is_in_stock is True
    assert normal_product.is_low_stock is False

    # limited_product only has v3 with 2 units -> low stock is True
    assert limited_product.is_in_stock is True
    assert limited_product.is_low_stock is True


def test_product_card_renders_low_stock_badge(client: Client, store_catalog):
    """Product cards in catalog should render the 'Low stock' badge for genuinely scarce items."""
    url = reverse("catalog:product-list")
    res = client.get(url)
    assert res.status_code == 200
    html = res.content.decode("utf-8")

    assert "Low stock" in html
    assert "Limited Edition Studio Tee" in html


def test_cart_mini_json_endpoint(client: Client, store_catalog):
    """The /shop/cart/mini/ endpoint should return valid JSON with dynamic threshold and items."""
    # 1. Empty cart test
    res = client.get(reverse("shop:cart-mini"))
    assert res.status_code == 200
    data = res.json()
    assert data["is_empty"] is True
    assert "free_shipping_threshold" in data
    assert "currency_symbol" in data
    assert Decimal(data["amount_for_free_shipping"]) > Decimal("0.00")

    # 2. Cart with item
    v1 = store_catalog["v1"]
    add_res = client.post(
        reverse("shop:cart-add"),
        {"variant_id": v1.pk, "quantity": 1},
        HTTP_ACCEPT="application/json",
    )
    assert add_res.status_code == 200

    mini_res = client.get(reverse("shop:cart-mini"))
    assert mini_res.status_code == 200
    mini_data = mini_res.json()
    assert mini_data["is_empty"] is False
    assert len(mini_data["items"]) == 1
    assert mini_data["items"][0]["product_name"] == store_catalog["product"].name
    assert mini_data["totals"]["item_count"] == 1


def test_cart_detail_dynamic_threshold(client: Client, store_catalog):
    """Cart detail page renders dynamic shipping threshold and progress meter."""
    v1 = store_catalog["v1"]
    client.post(reverse("shop:cart-add"), {"variant_id": v1.pk, "quantity": 1})

    url = reverse("shop:cart")
    res = client.get(url)
    assert res.status_code == 200
    html = res.content.decode("utf-8")

    assert "Free shipping threshold" in html
    assert "Order Summary" in html


def test_catalog_search_zero_results_and_active_filters(client: Client, store_catalog):
    """Catalog listing displays helpful zero-results guidance and active filter chips with removal links."""
    # Search for non-existent garment
    url = reverse("catalog:product-list") + "?q=xyznonexistentitem123"
    res = client.get(url)
    assert res.status_code == 200
    html = res.content.decode("utf-8")

    assert "No garments found for “xyznonexistentitem123”" in html
    assert "Try Visual Discovery" in html
    assert "Active filters:" in html
    assert "Clear all filters" in html


def test_checkout_place_order_double_submission_protection():
    """Checkout page should include double-submission prevention handler."""
    with open("templates/shop/checkout.html", "r", encoding="utf-8") as f:
        content = f.read()
    assert "Securing order..." in content
    assert "dataset.submitted" in content
