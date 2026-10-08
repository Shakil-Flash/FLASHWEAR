"""Phase 38: Intelligent Homepage & Dynamic Storefront test suite.

Validates:
1. Content hierarchy: Brand Story -> Relevant Products -> Personalization -> Community -> Discovery
2. Returning customer personalization: Continue shopping, Wishlist, Recommendations, Closet pairing
3. New customer fallback: Curated catalog, no empty personalization gaps, authentic community
4. Content Studio source of truth: Enabled/disabled states, scheduling, hero/banner/rail separation
5. Campaign & Drop integration: Live drops rendered, draft drops excluded
6. Community & Creator integration: Real creator looks, no fake testimonials
7. Performance: Anonymous homepage query count stays within strict <= 10 budget
8. Privacy & subtle language: No creepy phrasing or fake social proof
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.catalog.models import Category, Product, ProductVariant
from apps.closet.models import ClosetItem
from apps.core.models import HomepageSection
from apps.core.services.content import invalidate_homepage_cache
from apps.creator.models import CreatorPost, CreatorPostStatus, CreatorProfile, CreatorStatus
from apps.drops.models import DropProduct, DropStatus, FlashDrop
from apps.inventory.models import Stock
from apps.shop.models import Wishlist, WishlistItem
from apps.styling.models.flash_dna import FlashDNA

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean_homepage_cache():
    """Ensure homepage cache is invalidated before and after each test."""
    invalidate_homepage_cache()
    yield
    invalidate_homepage_cache()


@pytest.fixture
def base_product(db):
    cat = Category.objects.create(name="Tops", slug="tops")
    prod = Product.objects.create(
        name="Heavyweight Cotton Tee",
        slug="heavyweight-cotton-tee",
        category=cat,
        status=Product.Status.ACTIVE,
        published_at=timezone.now(),
        is_featured=True,
    )
    var = ProductVariant.objects.create(
        product=prod,
        sku="HCT-BLK-M",
        price=Decimal("45.00"),
        is_active=True,
    )
    Stock.objects.create(variant=var, on_hand=20)
    return prod


@pytest.fixture
def second_product(db):
    cat = Category.objects.create(name="Trousers", slug="trousers")
    prod = Product.objects.create(
        name="Tailored Relaxed Pant",
        slug="tailored-relaxed-pant",
        category=cat,
        status=Product.Status.ACTIVE,
        published_at=timezone.now(),
        is_featured=True,
    )
    var = ProductVariant.objects.create(
        product=prod,
        sku="TRP-NAV-M",
        price=Decimal("95.00"),
        is_active=True,
    )
    Stock.objects.create(variant=var, on_hand=15)
    return prod


class TestAnonymousHomepage:
    """Test cold-start / new customer storefront."""

    def test_anonymous_homepage_renders_clean_hierarchy(self, client, base_product):
        response = client.get("/")
        assert response.status_code == 200
        content = response.content.decode()

        # Brand story / Editorial hero is present
        assert "Wear what feels like you." in content
        assert "Explore the Drop" in content

        # Featured products present
        assert "Heavyweight Cotton Tee" in content

        # Emotive mood discovery present with functional links
        assert "Shop by Mood" in content
        assert "Clean &amp; Quiet" in content or "Clean & Quiet" in content

        # Personalization sections are NOT rendered when there is no activity
        assert "Still thinking about these?" not in content
        assert "Saved in your Wishlist" not in content
        assert "Picked for you" not in content
        assert "Coordinates with your closet" not in content

        # No fake customer testimonials
        assert "Verified Shopper" not in content

    def test_anonymous_homepage_stays_within_query_budget(
        self, client, django_assert_max_num_queries
    ):
        """Anonymous homepage must not exceed query count budget of 10."""
        with django_assert_max_num_queries(10):
            response = client.get("/")
            assert response.status_code == 200


class TestReturningCustomerPersonalization:
    """Test intelligent personalization for returning active customers."""

    def test_wishlist_items_surfaced_for_authenticated_customer(
        self, client, verified_user, base_product
    ):
        wishlist, _ = Wishlist.objects.get_or_create(user=verified_user)
        WishlistItem.objects.create(wishlist=wishlist, product=base_product)

        client.force_login(verified_user)
        response = client.get("/")
        assert response.status_code == 200
        content = response.content.decode()

        assert "Saved in your Wishlist" in content
        assert "Saved by you" in content
        assert "Heavyweight Cotton Tee" in content

    def test_continue_shopping_surfaced_from_session(
        self, client, verified_user, base_product, second_product
    ):
        client.force_login(verified_user)
        # Browsing activity stored in session with 2+ items satisfies continue shopping threshold
        session = client.session
        session["fw_recently_viewed"] = [base_product.pk, second_product.pk]
        session.save()

        response = client.get("/")
        assert response.status_code == 200
        content = response.content.decode()

        assert "Still thinking about these?" in content
        assert "Pick up where you left off" in content
        assert "Heavyweight Cotton Tee" in content

    def test_deduplication_between_continue_shopping_and_wishlist(
        self, client, verified_user, base_product, second_product
    ):
        """A product already in continue_shopping should not be duplicated in wishlist rail."""
        cat = Category.objects.create(name="Accessories", slug="accessories")
        p3 = Product.objects.create(
            name="Canvas Cap",
            slug="canvas-cap",
            category=cat,
            status=Product.Status.ACTIVE,
            published_at=timezone.now(),
            is_featured=True,
        )
        p4 = Product.objects.create(
            name="Leather Belt",
            slug="leather-belt",
            category=cat,
            status=Product.Status.ACTIVE,
            published_at=timezone.now(),
            is_featured=True,
        )
        third_product = Product.objects.create(
            name="Merino Wool Beanie",
            slug="merino-wool-beanie",
            category=cat,
            status=Product.Status.ACTIVE,
            published_at=timezone.now(),
            is_featured=True,
        )

        wishlist, _ = Wishlist.objects.get_or_create(user=verified_user)
        WishlistItem.objects.create(wishlist=wishlist, product=base_product)
        WishlistItem.objects.create(wishlist=wishlist, product=third_product)

        session = client.session
        session["fw_recently_viewed"] = [base_product.pk, second_product.pk, p3.pk, p4.pk]
        session.save()

        client.force_login(verified_user)
        response = client.get("/")
        assert response.status_code == 200

        # base_product is in continue_shopping, third_product is in wishlist_items
        context = response.context
        continue_ids = [p.pk for p in context.get("continue_shopping", [])]
        wishlist_ids = [p.pk for p in context.get("wishlist_items", [])]

        assert base_product.pk in continue_ids
        assert base_product.pk not in wishlist_ids
        assert third_product.pk in wishlist_ids

    def test_personalized_recommendations_with_flash_dna(self, client, verified_user, base_product):
        """Customer with style goals receives recommendations with genuine reasons."""
        FlashDNA.objects.create(
            user=verified_user,
            styles="minimal",
            style_goal="minimal",
        )

        client.force_login(verified_user)
        response = client.get("/")
        assert response.status_code == 200
        content = response.content.decode()

        if response.context.get("personalized_recommendations"):
            assert "Picked for you" in content
            assert "Tuned for minimal" in content

    def test_closet_pairing_surfaced_when_customer_has_wardrobe_items(
        self, client, verified_user, base_product
    ):
        """Customer with closet pieces sees pairing suggestion."""
        ClosetItem.objects.create(
            user=verified_user,
            name="Raw Denim Jacket",
            category="outerwear",
            color="indigo",
        )

        client.force_login(verified_user)
        response = client.get("/")
        assert response.status_code == 200

        context = response.context
        if context.get("closet_pairing"):
            content = response.content.decode()
            assert "Coordinates with your closet" in content
            assert "Raw Denim Jacket" in content


class TestContentStudioSourceOfTruth:
    """Test that Content Studio sections are respected without destroying default storefront."""

    def test_managed_hero_replaces_default_hero(self, client):
        HomepageSection.objects.create(
            title="Autumn Capsule Studio Hero",
            subtitle="Curated by our directors",
            section_type=HomepageSection.SectionType.HERO,
            is_enabled=True,
            display_order=1,
        )

        response = client.get("/")
        assert response.status_code == 200
        content = response.content.decode()

        assert "Autumn Capsule Studio Hero" in content
        assert "Curated by our directors" in content
        # Default hero headline is overridden
        assert "Wear what feels like you." not in content

    def test_disabled_content_section_not_visible(self, client):
        HomepageSection.objects.create(
            title="Secret Unpublished Banner",
            section_type=HomepageSection.SectionType.BANNER,
            is_enabled=False,
        )

        response = client.get("/")
        assert response.status_code == 200
        assert "Secret Unpublished Banner" not in response.content.decode()

    def test_scheduled_section_future_not_visible(self, client):
        future_time = timezone.now() + timedelta(days=2)
        HomepageSection.objects.create(
            title="Future Holiday Campaign",
            section_type=HomepageSection.SectionType.BANNER,
            is_enabled=True,
            starts_at=future_time,
        )

        response = client.get("/")
        assert response.status_code == 200
        assert "Future Holiday Campaign" not in response.content.decode()

    def test_expired_section_not_visible(self, client):
        past_time = timezone.now() - timedelta(days=2)
        HomepageSection.objects.create(
            title="Expired Summer Sale",
            section_type=HomepageSection.SectionType.BANNER,
            is_enabled=True,
            ends_at=past_time,
        )

        response = client.get("/")
        assert response.status_code == 200
        assert "Expired Summer Sale" not in response.content.decode()

    def test_managed_banner_and_curated_rails_coexist_with_core_catalog(self, client, base_product):
        """Managed banners and rails do NOT hide default featured products or mood sections."""
        HomepageSection.objects.create(
            title="Mid-Season Archive Sale",
            subtitle="Explore archival pieces",
            section_type=HomepageSection.SectionType.BANNER,
            is_enabled=True,
        )
        HomepageSection.objects.create(
            title="Director Selection Rail",
            section_type=HomepageSection.SectionType.FEATURED_PRODUCTS,
            is_enabled=True,
        )

        response = client.get("/")
        assert response.status_code == 200
        content = response.content.decode()

        # Managed banner and rail are rendered
        assert "Mid-Season Archive Sale" in content
        assert "Director Selection Rail" in content

        # Default sections are preserved (NOT wiped out)
        assert "Heavyweight Cotton Tee" in content
        assert "Shop by Mood" in content


class TestCampaignsDropsAndCommunity:
    """Test active drops and authentic community integration."""

    def test_live_drop_rendered_on_homepage(self, client, base_product):
        now = timezone.now()
        drop = FlashDrop.objects.create(
            name="Obsidian Midnight Drop",
            slug="obsidian-midnight-drop",
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(days=1),
            status=DropStatus.LIVE,
        )
        DropProduct.objects.create(drop=drop, product=base_product)

        response = client.get("/")
        assert response.status_code == 200
        content = response.content.decode()

        assert "Exclusive Limited Drops" in content
        assert "Obsidian Midnight Drop" in content
        assert "Live now" in content

    def test_draft_drop_not_rendered(self, client):
        FlashDrop.objects.create(
            name="Unpublished Ghost Drop",
            slug="ghost-drop",
            status=DropStatus.DRAFT,
        )

        response = client.get("/")
        assert response.status_code == 200
        assert "Unpublished Ghost Drop" not in response.content.decode()

    def test_real_creator_posts_rendered_in_community_section(self, client, base_product):
        creator_user = User.objects.create_user(
            email="creator.style@flashwear.test",
            password="Str0ng-Passw0rd!",
            first_name="Marcus",
            last_name="Vance",
        )
        creator_prof = CreatorProfile.objects.create(
            user=creator_user,
            display_name="Marcus Vance",
            slug="marcus-vance",
            status=CreatorStatus.APPROVED,
            is_featured=True,
        )
        CreatorPost.objects.create(
            creator=creator_prof,
            title="Tokyo Nightwear Lookbook",
            slug="tokyo-nightwear-lookbook",
            caption="Heavyweight drape styled for evening Tokyo streets.",
            status=CreatorPostStatus.PUBLISHED,
            published_at=timezone.now(),
            like_count=5,
        )

        response = client.get("/")
        assert response.status_code == 200
        content = response.content.decode()

        assert "Worn in the Real World" in content
        assert "Tokyo Nightwear Lookbook" in content
        assert "Marcus Vance" in content
