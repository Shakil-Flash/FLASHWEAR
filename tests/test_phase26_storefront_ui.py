from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.navigation import account_sections
from apps.catalog.models import ProductVariant
from apps.drops.models import FlashDrop
from apps.inventory.models import Stock
from apps.loop.models import LoopItem, ResaleListing


@pytest.mark.django_db
class TestStorefrontProductHelpers:
    def test_product_compare_at_and_discount_calculation(self, make_product):
        product = make_product(name="Premium Oversized Hoodie")
        v1 = ProductVariant.objects.create(
            product=product,
            sku="HOOD-BLK-M",
            price=Decimal("70.00"),
            compare_at_price=Decimal("100.00"),
        )
        Stock.objects.create(variant=v1, on_hand=15, reserved=0)

        assert product.is_in_stock is True
        compare_range = product.compare_at_range
        assert compare_range is not None
        assert compare_range[0] == Decimal("100.00")
        assert product.max_discount_percent == 30  # (100 - 70) / 100 * 100 = 30%

    def test_product_out_of_stock_state(self, make_product):
        product = make_product(name="Sold Out Tee")
        v = ProductVariant.objects.create(
            product=product,
            sku="TEE-WHT-S",
            price=Decimal("40.00"),
        )
        Stock.objects.create(variant=v, on_hand=0, reserved=0)
        assert product.is_in_stock is False
        assert product.max_discount_percent == 0


@pytest.mark.django_db
class TestStorefrontLayoutAndNavigation:
    def test_account_navigation_wishlist_and_studio_integration(self):
        sections = account_sections()
        labels = [str(item["label"]) for item in sections]
        assert "Wishlist" in labels
        assert "Style Studio" in labels
        assert "FLASH DNA" in labels

        wishlist_item = next(item for item in sections if str(item["label"]) == "Wishlist")
        assert wishlist_item["live"] is True
        assert wishlist_item["url"] == reverse("shop:wishlist")

    def test_navbar_and_footer_unified_links(self, client):
        response = client.get(reverse("core:home"))
        assert response.status_code == 200
        content = response.content.decode()

        # Navbar brand & search modal
        assert "FLASHWEAR" in content
        assert "searchOpen" in content
        assert "/api/v1/products/suggestions/" in content
        assert "Search by Image" in content

        # Unified links
        assert reverse("catalog:product-list") in content
        assert reverse("account:studio") in content
        assert reverse("loop:resale-list") in content

    def test_search_suggestions_api(self, client, make_product):
        product = make_product(name="Graphic Acid Wash Tee", slug="acid-wash-tee")
        response = client.get("/api/v1/products/suggestions/?q=acid")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert any(item["name"] == product.name for item in data)


@pytest.mark.django_db
class TestStorefrontFeatureIntegration:
    def test_homepage_drops_section_appears_only_when_active(self, client, db):
        # Without drops, section is not rendered
        resp1 = client.get(reverse("core:home"))
        assert "Exclusive Limited Drops" not in resp1.content.decode()

        # With active drop
        FlashDrop.objects.create(
            name="Cyber Midnight Capsule",
            slug="cyber-midnight-capsule",
            starts_at=timezone.now() - timezone.timedelta(hours=1),
        )
        resp2 = client.get(reverse("core:home"))
        content = resp2.content.decode()
        assert "Exclusive Limited Drops" in content
        assert "Cyber Midnight Capsule" in content

    def test_homepage_loop_section_appears_only_when_active(self, client, make_product, user):
        # Without resale items, section is not rendered
        resp1 = client.get(reverse("core:home"))
        assert "FLASH Loop Pre-Loved" not in resp1.content.decode()

        # With active verified resale item
        product = make_product(name="Vintage Utility Jacket")
        loop_item = LoopItem.objects.create(
            user=user,
            product=product,
            type=LoopItem.Type.RESALE,
            status=LoopItem.Status.LISTED,
            condition=LoopItem.Condition.LIKE_NEW,
        )
        ResaleListing.objects.create(
            seller=user,
            loop_item=loop_item,
            asking_price=Decimal("75.00"),
            status=ResaleListing.Status.ACTIVE,
        )
        resp2 = client.get(reverse("core:home"))
        content = resp2.content.decode()
        assert "FLASH Loop Pre-Loved" in content
        assert "Vintage Utility Jacket" in content

    def test_product_detail_page_mobile_sticky_bar(self, client, make_product):
        product = make_product(name="Tailored Wool Trousers", slug="wool-trousers")
        v = ProductVariant.objects.create(
            product=product,
            sku="TR-WL-32",
            price=Decimal("120.00"),
        )
        Stock.objects.create(variant=v, on_hand=8, reserved=0)
        response = client.get(product.get_absolute_url())
        assert response.status_code == 200
        content = response.content.decode()
        # Sticky mobile bar
        assert "variant-options" in content
        assert "Add to bag" in content

    def test_checkout_step_progress_breadcrumb(self, client, user):
        client.force_login(user)
        response = client.get(reverse("shop:checkout"))
        assert response.status_code in (200, 302)  # May redirect if cart is empty
        if response.status_code == 200:
            content = response.content.decode()
            assert "1. Bag" in content
            assert "2. Details" in content
            assert "3. Payment" in content
            assert "4. Done" in content

    def test_account_horizontal_tab_navigation(self, client, user):
        client.force_login(user)
        response = client.get(reverse("account:profile"))
        assert response.status_code == 200
        content = response.content.decode()
        # Responsive tab navigation
        assert "overflow-x-auto" in content
        assert "Style Studio" in content
        assert "Wishlist" in content
