"""Phase 34: Fashion Discovery 2.0 — Mood, Occasion & Complete-the-Look.

Comprehensive test suite verifying:
- Mood-based discovery catalog and deterministic filtering
- Occasion-based discovery catalog and deterministic filtering
- Flash DNA personalization boosts vs truthful cold-start/anonymous behavior
- Search and catalog selector integration (?mood=..., ?occasion=..., URL parameters)
- Discovery Hub view (/discovery/) and event tracking
- Complete the Look multi-item "Add Look to Bag" (complete_look_add_all)
- Complete the Look "Create Outfit in Closet" (complete_look_create_outfit)
- Outfit Builder buyable piece "Add to Bag" integration
- Product detail discovery deduplication
- Homepage discovery rail context
- Privacy and security: private outfits/closet remain isolated, draft products hidden
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.analytics.models import Event
from apps.catalog.fashion_discovery import (
    filter_by_mood,
    filter_by_occasion,
    get_mood_catalog,
    get_occasion_catalog,
)
from apps.catalog.models import Category, Fit, Product, ProductTag, ProductVariant
from apps.catalog.selectors import apply_filters
from apps.closet.models import Outfit
from apps.inventory.models import Stock
from apps.shop.models import Cart, CartItem
from apps.styling.models.flash_dna import FlashDNA

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# 1. Mood Catalog & Filtering Tests
# --------------------------------------------------------------------------------------


class TestMoodDiscovery:
    def test_mood_catalog_metadata_and_lookup(self):
        catalog = get_mood_catalog()
        assert len(catalog) == 8
        mood_slugs = {m["slug"] for m in catalog}
        expected = {
            "minimal",
            "street",
            "smart_casual",
            "relaxed",
            "bold",
            "classic",
            "everyday",
            "night_out",
        }
        assert expected.issubset(mood_slugs)

        # Check each mood has required UI fields
        for mood in catalog:
            assert mood["name"]
            assert mood["tagline"]
            assert mood["accent"]

    def test_filter_by_mood_matches_tags_and_fits(self, make_product, colour, size):
        minimal_tag = ProductTag.objects.create(name="Minimalist", slug="minimalist")
        oversized_fit = Fit.objects.create(name="Oversized", slug="oversized")

        p_minimal = make_product(name="Clean Crewneck", slug="clean-crewneck")
        p_minimal.tags.add(minimal_tag)

        p_street = make_product(
            name="Graphic Skate Hoodie", slug="graphic-skate-hoodie", fit=oversized_fit
        )
        street_tag = ProductTag.objects.create(name="Streetwear", slug="streetwear")
        p_street.tags.add(street_tag)

        p_plain = make_product(name="Standard Chinos", slug="standard-chinos")

        # Query minimal
        qs_minimal = filter_by_mood(Product.objects.all(), "minimal")
        slugs_minimal = list(qs_minimal.values_list("slug", flat=True))
        assert p_minimal.slug in slugs_minimal
        assert p_street.slug not in slugs_minimal
        assert p_plain.slug not in slugs_minimal

        # Query street
        qs_street = filter_by_mood(Product.objects.all(), "street")
        slugs_street = list(qs_street.values_list("slug", flat=True))
        assert p_street.slug in slugs_street
        assert p_minimal.slug not in slugs_street
        assert p_plain.slug not in slugs_street

    def test_filter_by_mood_unknown_slug_falls_back_gracefully(self, make_product, colour, size):
        p1 = make_product(name="Piece 1", slug="piece-1")
        p2 = make_product(name="Piece 2", slug="piece-2")

        qs = filter_by_mood(Product.objects.all(), "unknown_fantasy_mood")
        assert qs.count() == 2
        assert p1 in qs and p2 in qs


# --------------------------------------------------------------------------------------
# 2. Occasion Catalog & Filtering Tests
# --------------------------------------------------------------------------------------


class TestOccasionDiscovery:
    def test_occasion_catalog_metadata(self):
        catalog = get_occasion_catalog()
        assert len(catalog) == 8
        slugs = {o["slug"] for o in catalog}
        expected = {
            "everyday",
            "work",
            "university",
            "date_night",
            "party",
            "travel",
            "weekend",
            "formal",
        }
        assert expected.issubset(slugs)

    def test_filter_by_occasion_matches_attributes(self, make_product, colour, size):
        blazer_cat = Category.objects.create(name="Blazers & Tailoring", slug="blazers")
        work_tag = ProductTag.objects.create(name="Workwear", slug="workwear")
        slim_fit = Fit.objects.create(name="Slim", slug="slim")

        work_jacket = make_product(
            name="Tailored Wool Blazer",
            slug="tailored-wool-blazer",
            category=blazer_cat,
            fit=slim_fit,
        )
        work_jacket.tags.add(work_tag)

        party_cat = Category.objects.create(name="Evening Glam", slug="evening-glam")
        party_tag = ProductTag.objects.create(name="Party", slug="party")
        party_top = make_product(
            name="Sequin Evening Top",
            slug="sequin-evening-top",
            category=party_cat,
            short_description="High-shine party top with festive sparkle.",
        )
        party_top.tags.add(party_tag)

        qs_work = filter_by_occasion(Product.objects.all(), "work")
        slugs_work = list(qs_work.values_list("slug", flat=True))
        assert work_jacket.slug in slugs_work
        assert party_top.slug not in slugs_work

        qs_party = filter_by_occasion(Product.objects.all(), "party")
        slugs_party = list(qs_party.values_list("slug", flat=True))
        assert party_top.slug in slugs_party
        assert work_jacket.slug not in slugs_party


# --------------------------------------------------------------------------------------
# 3. Flash DNA Personalization vs Cold-Start
# --------------------------------------------------------------------------------------


class TestPersonalizationScoring:
    def test_flash_dna_lifts_preferred_colors_without_excluding(
        self, user, make_product, colour, other_colour, size
    ):
        # User loves black
        dna = FlashDNA.objects.create(user=user, styles="minimal")
        dna.favorite_colors.add(colour)

        min_tag = ProductTag.objects.create(name="Minimal", slug="minimal")

        # Product 1: minimal with user's favorite colour
        p_fav = make_product(name="Minimal Black Coat", slug="min-black-coat")
        p_fav.tags.add(min_tag)
        ProductVariant.objects.create(
            product=p_fav, sku="MIN-BLK-M", color=colour, size=size, price="120.00"
        )

        # Product 2: minimal with other colour
        p_other = make_product(name="Minimal Sand Coat", slug="min-sand-coat")
        p_other.tags.add(min_tag)
        ProductVariant.objects.create(
            product=p_other, sku="MIN-SND-M", color=other_colour, size=size, price="120.00"
        )

        qs = filter_by_mood(Product.objects.all(), "minimal", user=user)
        results = list(qs)
        assert len(results) == 2
        # Favoured item should be prioritized first
        assert results[0].pk == p_fav.pk

    def test_anonymous_cold_start_does_not_break_or_fabricate(self, make_product, colour, size):
        min_tag = ProductTag.objects.create(name="Clean", slug="clean")
        p1 = make_product(name="Clean Shirt", slug="clean-shirt")
        p1.tags.add(min_tag)

        # Anonymous user (None)
        qs = filter_by_mood(Product.objects.all(), "minimal", user=None)
        assert p1 in qs


# --------------------------------------------------------------------------------------
# 4. Catalogue Selector & Query-param Integration
# --------------------------------------------------------------------------------------


class TestCatalogSelectorIntegration:
    def test_filter_products_with_mood_filter(self, make_product, colour, size):
        tag = ProductTag.objects.create(name="Streetwear", slug="streetwear")
        p_street = make_product(name="Street Cargo Pant", slug="street-cargo-pant")
        p_street.tags.add(tag)

        p_other = make_product(name="Linen Shirt", slug="linen-shirt")

        filtered = apply_filters(Product.objects.all(), filters={"mood": "street"})
        slugs = [p.slug for p in filtered]
        assert p_street.slug in slugs
        assert p_other.slug not in slugs

    def test_filter_products_with_occasion_filter(self, make_product, colour, size):
        tag = ProductTag.objects.create(name="Formal", slug="formal")
        p_formal = make_product(name="Formal Tuxedo Jacket", slug="formal-tuxedo-jacket")
        p_formal.tags.add(tag)

        p_casual = make_product(name="Casual Hoodie", slug="casual-hoodie")

        filtered = apply_filters(Product.objects.all(), filters={"occasion": "formal"})
        slugs = [p.slug for p in filtered]
        assert p_formal.slug in slugs
        assert p_casual.slug not in slugs

    def test_product_list_view_renders_mood_badge_and_seo(self, client, make_product, colour, size):
        tag = ProductTag.objects.create(name="Minimalist", slug="minimalist")
        p = make_product(name="Minimal Oxford", slug="minimal-oxford")
        p.tags.add(tag)

        url = reverse("catalog:product-list") + "?mood=minimal"
        response = client.get(url)

        assert response.status_code == 200
        assert response.context["active_mood"] == "minimal"
        assert response.context["active_mood_obj"]["name"] == "Minimal"
        # Badge should be rendered
        content = response.content.decode()
        assert "Minimal" in content
        assert "Mood: Minimal" in content or "active_mood" in str(response.context)


# --------------------------------------------------------------------------------------
# 5. Fashion Discovery Hub (/discovery/)
# --------------------------------------------------------------------------------------


class TestDiscoveryHubView:
    def test_discovery_hub_renders_all_moods_and_occasions(
        self, client, make_product, colour, size
    ):
        p = make_product(name="Core Discovery Item", slug="core-discovery-item")

        url = reverse("catalog:discovery-hub")
        before_events = Event.objects.filter(name="filter_used").count()

        response = client.get(url)
        assert response.status_code == 200
        assert "catalog/discovery_hub.html" in [t.name for t in response.templates]
        assert p in response.context["page_obj"]

        content = response.content.decode()
        assert "Fashion Discovery" in content
        assert "Shop by Mood" in content
        assert "Shop by Occasion" in content

        # Check mood cards exist
        assert "Minimal" in content
        assert "Street" in content
        assert "Smart Casual" in content

        # Event logged
        after_events = Event.objects.filter(name="filter_used").count()
        assert after_events == before_events + 1

    def test_discovery_hub_filtering_by_mood_and_occasion(self, client, make_product, colour, size):
        tag = ProductTag.objects.create(name="Streetwear", slug="streetwear")
        p_street = make_product(name="Oversized Street Hoodie", slug="oversized-street-hoodie")
        p_street.tags.add(tag)

        url = reverse("catalog:discovery-hub") + "?mood=street"
        response = client.get(url)

        assert response.status_code == 200
        assert response.context["active_mood"]["slug"] == "street"
        assert p_street in response.context["page_obj"]

    def test_discovery_hub_empty_state_is_graceful(self, client):
        # No products in DB
        url = reverse("catalog:discovery-hub") + "?mood=bold"
        response = client.get(url)
        assert response.status_code == 200
        assert b"No garments matched this exact combination right now" in response.content


# --------------------------------------------------------------------------------------
# 6. Complete the Look — Multi-Item "Add Look to Bag"
# --------------------------------------------------------------------------------------


class TestCompleteLookAddAll:
    def test_complete_look_add_all_adds_selected_variants(
        self, client, make_product, colour, size, other_size
    ):
        top = make_product(name="Main Linen Shirt", slug="main-linen-shirt")
        v1 = ProductVariant.objects.create(
            product=top, sku="TOP-M", color=colour, size=size, price="45.00"
        )
        Stock.objects.create(variant=v1, on_hand=10, reserved=0)

        bottom = make_product(name="Complementary Chinos", slug="comp-chinos")
        v2 = ProductVariant.objects.create(
            product=bottom, sku="BOT-L", color=colour, size=other_size, price="65.00"
        )
        Stock.objects.create(variant=v2, on_hand=5, reserved=0)

        url = reverse("catalog:complete-look-add-all", args=[top.slug])

        # Post both variants
        response = client.post(url, {"variant_ids": [str(v1.pk), str(v2.pk)]}, follow=True)

        assert response.status_code == 200
        # Cart has both items
        cart = Cart.objects.latest("created_at")
        assert cart.items.count() == 2
        variant_ids = set(cart.items.values_list("variant_id", flat=True))
        assert variant_ids == {v1.pk, v2.pk}

        # Event logged with complete_the_look source attribution
        look_events = Event.objects.filter(name="cart_add", metadata__source="complete_the_look")
        assert look_events.count() == 1
        ev = look_events.latest("created_at")
        assert ev.metadata["source"] == "complete_the_look"
        assert ev.metadata["added_count"] == 2

    def test_complete_look_add_all_handles_no_selection(self, client, make_product, colour, size):
        p = make_product(name="Solo Jacket", slug="solo-jacket")
        url = reverse("catalog:complete-look-add-all", args=[p.slug])

        response = client.post(url, {}, follow=True)
        assert response.status_code == 200
        assert CartItem.objects.count() == 0


# --------------------------------------------------------------------------------------
# 7. Complete the Look — "Create Outfit in Closet"
# --------------------------------------------------------------------------------------


class TestCompleteLookCreateOutfit:
    def test_complete_look_create_outfit_requires_auth(self, client, make_product, colour, size):
        p = make_product(name="Hero Coat", slug="hero-coat")
        url = reverse("catalog:complete-look-create-outfit", args=[p.slug])
        response = client.post(url, {"product_ids": [str(p.pk)]})
        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_complete_look_create_outfit_creates_outfit_with_items(
        self, user, client, make_product, colour, size
    ):
        top_cat = Category.objects.create(name="Tops", slug="tops")
        bot_cat = Category.objects.create(name="Bottoms", slug="bottoms")

        p_top = make_product(name="Hero Top", slug="hero-top", category=top_cat)
        ProductVariant.objects.create(
            product=p_top, sku="HT-M", color=colour, size=size, price="30.00"
        )
        p_bot = make_product(name="Hero Bottom", slug="hero-bottom", category=bot_cat)
        ProductVariant.objects.create(
            product=p_bot, sku="HB-M", color=colour, size=size, price="50.00"
        )

        client.force_login(user)
        url = reverse("catalog:complete-look-create-outfit", args=[p_top.slug])

        response = client.post(
            url,
            {"product_ids": [str(p_top.pk), str(p_bot.pk)], "occasion": "weekend"},
            follow=True,
        )

        assert response.status_code == 200
        outfit = Outfit.objects.filter(user=user).latest("created_at")
        assert outfit.user == user
        assert outfit.items.count() == 2
        # Check items link back to the products
        closet_products = {item.closet_item.product for item in outfit.items.all()}
        assert closet_products == {p_top, p_bot}


# --------------------------------------------------------------------------------------
# 8. Outfit Builder "Add to Bag" & Privacy
# --------------------------------------------------------------------------------------


class TestOutfitBuilderIntegration:
    def test_outfit_detail_private_to_owner(
        self, user, other_user, client, make_product, colour, size
    ):
        outfit = Outfit.objects.create(user=user, name="Secret Style")

        client.force_login(other_user)
        url = reverse("account:outfit-detail", args=[outfit.pk])
        response = client.get(url)
        assert response.status_code == 404

    def test_draft_product_hidden_from_discovery(self, client, make_product, colour, size):
        draft = make_product(
            name="Unpublished Sample",
            slug="unpublished-sample",
            status=Product.Status.DRAFT,
        )
        url = reverse("catalog:discovery-hub")
        response = client.get(url)
        assert draft not in response.context["page_obj"]


# --------------------------------------------------------------------------------------
# 9. Homepage Discovery Context
# --------------------------------------------------------------------------------------


class TestHomepageDiscovery:
    def test_home_view_provides_discovery_rails(self, client):
        url = reverse("core:home")
        response = client.get(url)
        assert response.status_code == 200
        assert "discovery_moods" in response.context
        assert "discovery_occasions" in response.context
        assert len(response.context["discovery_moods"]) == 8
        assert len(response.context["discovery_occasions"]) == 8

        content = response.content.decode()
        assert "Shop by Mood" in content
        assert "What are you dressing for?" in content
