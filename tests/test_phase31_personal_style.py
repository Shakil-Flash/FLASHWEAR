"""Phase 31: Personal Style & Customer Home test suite.

Validates:
1. Personalized Customer Home (fashion profile, continuity, sections)
2. Cold-start behavior (no fake data, calibration prompt, fresh drops)
3. "Your Style" summary (FLASH DNA integration without technical scores/percentages)
4. Style evolution (grounded data shifts vs insufficient data silence)
5. Personal recommendations presentation (fashion reasons, zero algorithm labels)
6. Closet & Outfit connection (PDP works with closet, outfit continuity)
7. Wishlist intelligence (stock, price drops, recency without fake urgency)
8. Privacy, customer data ownership and respectful tone
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Fit, ProductVariant
from apps.closet.models import ClosetItem, Outfit
from apps.closet.services.matching import get_closet_matches_for_product
from apps.inventory.models import Stock
from apps.shop.models import Wishlist, WishlistItem
from apps.styling.models.flash_dna import FlashDNA
from apps.styling.services.style_profile import (
    get_style_evolution_insight,
    get_style_profile_summary,
)

pytestmark = pytest.mark.django_db


class TestPersonalizedCustomerHome:
    def test_dashboard_requires_login(self, client):
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response["Location"]

    def test_customer_home_renders_fashion_profile(self, client, verified_user):
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Fashion Profile" in content
        assert verified_user.email in content
        assert "FLASH Points" in content

    def test_unverified_banner_shown_for_unverified_customer(self, client, user):
        client.force_login(user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        assert b"not verified" in response.content.lower()

    def test_unverified_banner_hidden_for_verified_customer(self, client, verified_user):
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        assert b"not verified" not in response.content.lower()

    def test_all_account_navigation_landmarks_preserved(self, client, verified_user):
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        content = response.content.decode()
        assert reverse("account:orders") in content
        assert reverse("account:closet") in content
        assert reverse("account:outfits") in content
        assert reverse("shop:wishlist") in content

    def test_at_a_glance_and_preferences_preserved(self, client, verified_user):
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        content = response.content.decode()
        assert "At a glance" in content
        assert "Preferences" in content
        assert "Saved addresses" in content


class TestColdStartExperience:
    def test_cold_start_displays_style_calibration_cta(self, client, verified_user):
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Help FLASHWEAR understand your personal style" in content
        assert "Define FLASH DNA" in content

    def test_cold_start_does_not_display_fake_evolution(self, client, verified_user):
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        content = response.content.decode()
        assert "Style Evolution" not in content

    def test_cold_start_does_not_invent_style_summary(self, client, verified_user):
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        content = response.content.decode()
        assert "Your Style Profile" not in content

    def test_cold_start_shows_fresh_drops_exploration(self, client, verified_user, make_product):
        p1 = make_product(name="Fresh Arrival Jacket")
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        assert p1.name.encode() in response.content


class TestYourStyleSummary:
    def test_style_profile_summary_service_extracts_clean_attributes(self, verified_user, colour):
        dna = FlashDNA.objects.create(
            user=verified_user,
            styles="Streetwear, Minimalist",
            preferred_categories="tops",
            preferred_seasons="autumn",
            style_goal="classic",
        )
        dna.favorite_colors.add(colour)
        relaxed_fit = Fit.objects.create(name="Relaxed", slug="relaxed-summary-fit")
        dna.preferred_fits.add(relaxed_fit)

        summary = get_style_profile_summary(verified_user)
        assert summary["is_complete"] is True
        assert "Streetwear" in summary["styles"]
        assert "Minimalist" in summary["styles"]
        assert colour in summary["favorite_colors"]
        assert relaxed_fit in summary["preferred_fits"]
        assert "Tops" in summary["category_focus"]
        assert "Classic" in summary["style_goal_label"]

    def test_style_summary_renders_colors_fits_and_styles(self, client, verified_user, colour):
        dna = FlashDNA.objects.create(
            user=verified_user,
            styles="Streetwear",
            preferred_categories="tops",
            style_goal="classic",
        )
        dna.favorite_colors.add(colour)
        fit = Fit.objects.create(name="Relaxed", slug="fit-relaxed-test")
        dna.preferred_fits.add(fit)

        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Your Style Profile" in content
        assert "Streetwear" in content
        assert colour.name in content
        assert "Relaxed" in content

    def test_style_summary_never_exposes_internal_scoring_or_percentages(
        self, client, verified_user
    ):
        FlashDNA.objects.create(
            user=verified_user,
            styles="Streetwear",
            style_goal="classic",
        )
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        content = response.content.decode()
        # Verify no raw scoring artifacts
        assert "dna_score" not in content
        assert "vector" not in content.lower()
        assert "confidence_score" not in content


class TestStyleEvolution:
    def test_style_evolution_insufficient_data_returns_no_conclusion(self, verified_user):
        evolution = get_style_evolution_insight(verified_user)
        assert evolution["has_data"] is False
        assert evolution["summary"] == ""

    def test_style_evolution_detects_relaxed_silhouette_shift(self, verified_user, make_product):
        relaxed_fit = Fit.objects.create(name="Relaxed", slug="relaxed-test")
        p1 = make_product(name="Relaxed Hoodie", fit=relaxed_fit)
        p2 = make_product(name="Boxy Oversized Tee", fit=relaxed_fit)

        var1 = ProductVariant.objects.create(product=p1, sku="RLX-1", price=Decimal("60.00"))
        var2 = ProductVariant.objects.create(product=p2, sku="RLX-2", price=Decimal("40.00"))

        ClosetItem.objects.create(
            user=verified_user,
            category=ClosetItem.Category.TOPS,
            name="Relaxed Hoodie",
            variant=var1,
            status=ClosetItem.Status.ACTIVE,
        )
        ClosetItem.objects.create(
            user=verified_user,
            category=ClosetItem.Category.TOPS,
            name="Boxy Tee",
            variant=var2,
            status=ClosetItem.Status.ACTIVE,
        )

        evolution = get_style_evolution_insight(verified_user)
        assert evolution["has_data"] is True
        assert "relaxed" in evolution["summary"].lower()

    def test_style_evolution_detects_monochrome_palette_shift(
        self, verified_user, make_product, colour
    ):
        p1 = make_product(name="Black Tee")
        p2 = make_product(name="Charcoal Shirt")
        p3 = make_product(name="White Overshirt")

        var1 = ProductVariant.objects.create(
            product=p1, sku="MONO-1", color=colour, price=Decimal("30.00")
        )
        var2 = ProductVariant.objects.create(
            product=p2, sku="MONO-2", color=colour, price=Decimal("45.00")
        )
        var3 = ProductVariant.objects.create(
            product=p3, sku="MONO-3", color=colour, price=Decimal("50.00")
        )

        ClosetItem.objects.create(
            user=verified_user,
            category=ClosetItem.Category.TOPS,
            name="Black Tee",
            variant=var1,
            color="black",
            status=ClosetItem.Status.ACTIVE,
        )
        ClosetItem.objects.create(
            user=verified_user,
            category=ClosetItem.Category.TOPS,
            name="Charcoal Shirt",
            variant=var2,
            color="charcoal",
            status=ClosetItem.Status.ACTIVE,
        )
        ClosetItem.objects.create(
            user=verified_user,
            category=ClosetItem.Category.TOPS,
            name="White Overshirt",
            variant=var3,
            color="white",
            status=ClosetItem.Status.ACTIVE,
        )

        evolution = get_style_evolution_insight(verified_user)
        assert evolution["has_data"] is True
        summary_lower = evolution["summary"].lower()
        assert "monochrome" in summary_lower or "minimalist" in summary_lower


class TestRecommendationsPresentation:
    def test_recommendations_human_fashion_reasons(self, client, verified_user, make_product):
        FlashDNA.objects.create(
            user=verified_user,
            styles="Streetwear",
            preferred_categories="tops",
            style_goal="classic",
        )
        make_product(name="Styling Recommendation Item")
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Recommended For Your Style" in content
        # Reasons must not be internal codes
        assert "dna_category" not in content
        assert "dna_style" not in content

    def test_recommendations_presentation_never_leaks_algorithm_labels(
        self, client, verified_user, make_product
    ):
        make_product(name="Test Jacket")
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        content = response.content.decode()
        # Ensure no percentage values like "(45%)" leak into recommendation reasons
        assert "45%" not in content
        assert "50%" not in content


class TestClosetAndOutfitIntegration:
    def test_closet_matching_service_finds_complementary_pieces(self, verified_user, make_product):
        top_product = make_product(name="Oversized Linen Shirt")
        bottom_item = ClosetItem.objects.create(
            user=verified_user,
            category=ClosetItem.Category.BOTTOMS,
            name="Relaxed Chinos",
            status=ClosetItem.Status.ACTIVE,
        )

        matches = get_closet_matches_for_product(verified_user, top_product)
        assert len(matches) >= 1
        assert matches[0] == bottom_item

    def test_product_detail_shows_works_with_closet_for_auth_user(
        self, client, verified_user, make_product
    ):
        shirt = make_product(name="Cotton Oxford Shirt")
        ClosetItem.objects.create(
            user=verified_user,
            category=ClosetItem.Category.BOTTOMS,
            name="Pleated Trousers",
            status=ClosetItem.Status.ACTIVE,
        )

        client.force_login(verified_user)
        response = client.get(shirt.get_absolute_url())
        assert response.status_code == 200
        content = response.content.decode()
        assert "Works with" in content
        assert "in your closet" in content
        assert "Pleated Trousers" in content

    def test_product_detail_omits_closet_section_for_anonymous_user(self, client, make_product):
        shirt = make_product(name="Cotton Oxford Shirt")
        response = client.get(shirt.get_absolute_url())
        assert response.status_code == 200
        assert b"in your closet" not in response.content

    def test_outfit_continuity_prompts_draft_outfit(self, client, verified_user):
        draft = Outfit.objects.create(
            user=verified_user,
            name="Weekend Casual Concept",
            status=Outfit.Status.DRAFT,
        )
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Unfinished Outfit" in content
        assert draft.name in content


class TestWishlistIntelligence:
    def test_wishlist_intelligence_flags_low_stock(self, client, verified_user, make_product):
        p = make_product(name="Exclusive Silk Shirt")
        v = ProductVariant.objects.create(product=p, sku="SILK-1", price=Decimal("120.00"))
        Stock.objects.create(variant=v, on_hand=3, reserved=0)

        wishlist = Wishlist.objects.create(user=verified_user)
        WishlistItem.objects.create(wishlist=wishlist, product=p, variant=v)

        client.force_login(verified_user)
        response = client.get(reverse("shop:wishlist"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Low stock: 3 left" in content

    def test_wishlist_intelligence_flags_price_drop(self, client, verified_user, make_product):
        p = make_product(name="Discounted Trench")
        v = ProductVariant.objects.create(
            product=p,
            sku="TRENCH-1",
            price=Decimal("150.00"),
            compare_at_price=Decimal("200.00"),
        )
        Stock.objects.create(variant=v, on_hand=10, reserved=0)

        wishlist = Wishlist.objects.create(user=verified_user)
        WishlistItem.objects.create(wishlist=wishlist, product=p, variant=v)

        client.force_login(verified_user)
        response = client.get(reverse("shop:wishlist"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Price drop" in content

    def test_wishlist_intelligence_flags_sold_out(self, client, verified_user, make_product):
        p = make_product(name="Sold Out Boots")
        v = ProductVariant.objects.create(product=p, sku="BOOT-1", price=Decimal("180.00"))
        Stock.objects.create(variant=v, on_hand=0, reserved=0)

        wishlist = Wishlist.objects.create(user=verified_user)
        WishlistItem.objects.create(wishlist=wishlist, product=p, variant=v)

        client.force_login(verified_user)
        response = client.get(reverse("shop:wishlist"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Sold out" in content

    def test_wishlist_intelligence_flags_recently_added(self, client, verified_user, make_product):
        p = make_product(name="Recent Sneaker")
        v = ProductVariant.objects.create(product=p, sku="SNK-1", price=Decimal("90.00"))
        Stock.objects.create(variant=v, on_hand=20, reserved=0)

        wishlist = Wishlist.objects.create(user=verified_user)
        item = WishlistItem.objects.create(wishlist=wishlist, product=p, variant=v)
        item.created_at = timezone.now() - timedelta(days=2)
        item.save()

        client.force_login(verified_user)
        response = client.get(reverse("shop:wishlist"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Recently added" in content

    def test_wishlist_preview_on_customer_home(self, client, verified_user, make_product):
        p = make_product(name="Home Preview Tee")
        v = ProductVariant.objects.create(product=p, sku="PREV-1", price=Decimal("35.00"))
        Stock.objects.create(variant=v, on_hand=4, reserved=0)

        wishlist = Wishlist.objects.create(user=verified_user)
        WishlistItem.objects.create(wishlist=wishlist, product=p, variant=v)

        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Wishlist Intelligence" in content
        assert "Low stock: 4 left" in content
        assert p.name in content


class TestPrivacyAndDataOwnership:
    def test_customer_cannot_see_another_customers_closet_or_style(
        self, client, verified_user, other_user, make_product
    ):
        ClosetItem.objects.create(
            user=other_user,
            category=ClosetItem.Category.TOPS,
            name="Secret Vintage Leather Jacket",
            status=ClosetItem.Status.ACTIVE,
        )

        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        assert response.status_code == 200
        assert b"Secret Vintage Leather Jacket" not in response.content

    def test_privacy_controls_section_provides_clear_links(self, client, verified_user):
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        content = response.content.decode()
        assert "Your Style & Personalization Controls" in content
        assert reverse("account:studio") in content
        assert reverse("account:notifications") in content
        assert reverse("account:security") in content

    def test_no_creepy_tracking_language_in_templates(self, client, verified_user):
        client.force_login(verified_user)
        response = client.get(reverse("account:dashboard"))
        content = response.content.decode().lower()
        assert "tracking you" not in content
        assert "tracking everything" not in content
        assert "watching you" not in content
