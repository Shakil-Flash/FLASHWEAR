"""Phase 32 tests: Community, Social Proof & Shoppable UGC.

Tests:
- Approved creator and published post visibility
- Draft/pending/rejected moderation rules (never exposed publicly)
- Product detail community content ("Styled by the community", "Seen in real looks")
- Shoppable UGC tags (creator post -> product)
- Verified purchase display & customer privacy protection on reviews
- Community feed (/inspiration/) and filtering by category/mood
- Likes and saves toggling & real count integrity
- Creator directory & profile discovery
- Homepage Content Studio creator section integration
- Empty state honesty
"""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.catalog.models import Category, Product
from apps.closet.models import ClosetItem, Outfit, OutfitItem
from apps.core.models import HomepageSection
from apps.creator.models import (
    CreatorApplication,
    CreatorPost,
    CreatorPostLike,
    CreatorPostStatus,
    CreatorProfile,
    CreatorStatus,
)
from apps.creator.services import (
    add_outfit_tag,
    add_product_tag,
)
from apps.orders.models import Order

pytestmark = pytest.mark.django_db


@pytest.fixture
def creator_user(db):
    return get_user_model().objects.create_user(
        email="creator@flashwear.test",
        password="Str0ng-Passw0rd!",
        first_name="Elena",
        last_name="Rostova",
    )


@pytest.fixture
def approved_creator(creator_user):
    return CreatorProfile.objects.create(
        user=creator_user,
        display_name="Elena Rostova",
        slug="elena-rostova",
        bio="Minimalist aesthetics and architectural tailoring.",
        location="Berlin",
        status=CreatorStatus.APPROVED,
        is_featured=True,
    )


@pytest.fixture
def published_post(approved_creator, product):
    post = CreatorPost.objects.create(
        creator=approved_creator,
        title="Oversized Layering for Autumn",
        slug="oversized-layering-autumn",
        caption="Styling the heavyweight tee with pleated trousers and combat boots.",
        status=CreatorPostStatus.PUBLISHED,
        like_count=5,
        save_count=2,
    )
    add_product_tag(post, product, label="Core Base Layer")
    return post


# -----------------------------------------------------------------------------
# 1. Moderation & Visibility Rules
# -----------------------------------------------------------------------------


def test_approved_creator_and_published_post_are_visible_publicly(
    client, published_post, approved_creator
):
    # Inspiration feed lists published post
    res = client.get(reverse("inspiration:list"))
    assert res.status_code == 200
    assert published_post.title in res.content.decode()
    assert approved_creator.display_name in res.content.decode()

    # Look detail page answers 200
    res_post = client.get(reverse("inspiration:detail", kwargs={"slug": published_post.slug}))
    assert res_post.status_code == 200
    assert published_post.title in res_post.content.decode()

    # Creator profile answers 200
    url = reverse("creator:creator_detail", kwargs={"slug": approved_creator.slug})
    res_profile = client.get(url)
    assert res_profile.status_code == 200
    assert approved_creator.display_name in res_profile.content.decode()


def test_draft_posts_and_pending_creators_are_hidden_from_public(client, creator_user, other_user):
    # Pending creator profile
    pending_creator = CreatorProfile.objects.create(
        user=other_user,
        display_name="Pending Stylist",
        slug="pending-stylist",
        status=CreatorStatus.PENDING,
    )
    # Draft post
    draft_post = CreatorPost.objects.create(
        creator=pending_creator,
        title="Secret Unreleased Look",
        slug="secret-unreleased-look",
        status=CreatorPostStatus.DRAFT,
    )

    # 404 for public visitor on creator detail
    detail_url = reverse("creator:creator_detail", kwargs={"slug": pending_creator.slug})
    assert client.get(detail_url).status_code == 404

    # 404 for public visitor on post detail
    post_url = reverse("inspiration:detail", kwargs={"slug": draft_post.slug})
    assert client.get(post_url).status_code == 404

    # Neither appears on inspiration feed or creator list
    feed_content = client.get(reverse("inspiration:list")).content.decode()
    assert draft_post.title not in feed_content

    dir_content = client.get(reverse("creator:creator_list")).content.decode()
    assert pending_creator.display_name not in dir_content


# -----------------------------------------------------------------------------
# 2. Product Detail Community Content & Shoppable UGC
# -----------------------------------------------------------------------------


def test_product_detail_displays_community_posts_when_tagged(client, product, published_post):
    res = client.get(product.get_absolute_url())
    assert res.status_code == 200
    content = res.content.decode()

    # "Styled by the Community" section is rendered
    assert "Styled by the Community" in content
    assert published_post.title in content
    assert published_post.creator.display_name in content
    assert "Core Base Layer" in content


def test_product_detail_omits_community_section_when_no_content_exists(client, product):
    # Product without any creator tags
    res = client.get(product.get_absolute_url())
    assert res.status_code == 200
    content = res.content.decode()

    # No empty or broken section rendered
    assert "Styled by the Community" not in content
    assert "Seen in Real Looks" not in content


def test_product_detail_displays_community_outfits(client, product, published_post, creator_user):
    # Create outfit tagged to creator post
    outfit = Outfit.objects.create(
        user=creator_user,
        name="Urban Minimal Autumn Outfit",
        description="Relaxed silhouette for cold city days.",
        status=Outfit.Status.SAVED,
    )
    variant = product.variants.first()
    closet_item = ClosetItem.objects.create(
        user=creator_user,
        variant=variant,
        name=product.name,
        category=ClosetItem.Category.TOPS,
    )
    OutfitItem.objects.create(outfit=outfit, closet_item=closet_item, role="top", position=1)
    add_outfit_tag(published_post, outfit)

    res = client.get(product.get_absolute_url())
    assert res.status_code == 200
    content = res.content.decode()

    assert "Seen in Real Looks" in content
    assert "Urban Minimal Autumn Outfit" in content


def test_creator_look_detail_shows_shoppable_products(client, published_post, product):
    res = client.get(reverse("inspiration:detail", kwargs={"slug": published_post.slug}))
    assert res.status_code == 200
    content = res.content.decode()

    assert "Shop This Look" in content
    assert product.name in content
    assert product.get_absolute_url() in content
    assert "Core Base Layer" in content


# -----------------------------------------------------------------------------
# 3. Inspiration Feed & Filtering
# -----------------------------------------------------------------------------


def test_inspiration_feed_category_and_mood_filters(client, approved_creator, product):
    cat_hoodies = Category.objects.create(name="Hoodies", slug="hoodies", is_active=True)
    product_hoodie = Product.objects.create(
        name="Heavy Cyber Hoodie",
        slug="heavy-cyber-hoodie",
        category=cat_hoodies,
        status="active",
        published_at=product.published_at,
    )
    post_hoodie = CreatorPost.objects.create(
        creator=approved_creator,
        title="Cyber Street Style",
        slug="cyber-street-style",
        status=CreatorPostStatus.PUBLISHED,
    )
    add_product_tag(post_hoodie, product_hoodie)

    # Filter by category=hoodies
    res = client.get(reverse("inspiration:list"), {"category": "hoodies"})
    assert res.status_code == 200
    content = res.content.decode()
    assert "Cyber Street Style" in content

    # Filter by non-existent category shows honest empty state
    res_empty = client.get(reverse("inspiration:list"), {"category": "outerwear"})
    assert res_empty.status_code == 200
    assert "No community looks found" in res_empty.content.decode()


# -----------------------------------------------------------------------------
# 4. Likes and Saves with Real Engagement Signals
# -----------------------------------------------------------------------------


def test_authenticated_user_can_like_and_unlike_creator_post(client, user, published_post):
    client.force_login(user)
    initial_likes = published_post.like_count

    # Like
    res = client.post(
        reverse("inspiration:like", kwargs={"slug": published_post.slug}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert res.status_code == 200
    data = res.json()
    assert data["liked"] is True
    assert data["like_count"] == initial_likes + 1
    assert CreatorPostLike.objects.filter(user=user, post=published_post).exists()

    # Unlike
    res_unlike = client.post(
        reverse("inspiration:like", kwargs={"slug": published_post.slug}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert res_unlike.status_code == 200
    data_unlike = res_unlike.json()
    assert data_unlike["liked"] is False
    assert data_unlike["like_count"] == initial_likes
    assert not CreatorPostLike.objects.filter(user=user, post=published_post).exists()


def test_authenticated_user_can_save_and_unsave_creator_post(client, user, published_post):
    client.force_login(user)
    initial_saves = published_post.save_count

    # Save
    res = client.post(
        reverse("inspiration:save", kwargs={"slug": published_post.slug}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert res.status_code == 200
    data = res.json()
    assert data["saved"] is True
    assert data["save_count"] == initial_saves + 1

    # Unsave
    res_unsave = client.post(
        reverse("inspiration:save", kwargs={"slug": published_post.slug}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert res_unsave.status_code == 200
    data_unsave = res_unsave.json()
    assert data_unsave["saved"] is False
    assert data_unsave["save_count"] == initial_saves


def test_unauthenticated_user_cannot_like_or_save(client, published_post):
    res_like = client.post(
        reverse("inspiration:like", kwargs={"slug": published_post.slug}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert res_like.status_code == 401

    res_save = client.post(
        reverse("inspiration:save", kwargs={"slug": published_post.slug}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert res_save.status_code == 401


# -----------------------------------------------------------------------------
# 5. Reviews + Community Integration & Customer Privacy
# -----------------------------------------------------------------------------


def test_verified_purchase_indicator_and_privacy_on_reviews(client, product, user):
    from tests.phase6_helpers import placed_order
    from tests.phase7_helpers import make_review

    variant = product.variants.first()
    order = placed_order(user, variant)
    order.status = Order.Status.PAID
    order.save(update_fields=["status"])

    make_review(
        order=order,
        user=user,
        product=product,
        rating=5,
        title="Flawless heavy cotton",
        body="Exceptional weight and drape. Sits cleanly with wide trousers.",
        verified=True,
    )

    res = client.get(product.get_absolute_url())
    assert res.status_code == 200
    content = res.content.decode()

    # Verified purchase badge is displayed
    assert "Verified purchase" in content
    assert "Flawless heavy cotton" in content

    # Private customer email is NOT leaked
    assert user.email not in content


# -----------------------------------------------------------------------------
# 6. Creator Discovery & Profiles
# -----------------------------------------------------------------------------


def test_creator_profile_page_displays_all_looks_and_wardrobe(
    client, approved_creator, published_post, product
):
    res = client.get(reverse("creator:creator_detail", kwargs={"slug": approved_creator.slug}))
    assert res.status_code == 200
    content = res.content.decode()

    assert approved_creator.display_name in content
    assert approved_creator.bio in content
    assert published_post.title in content
    # Tagged product appears in wardrobe selection
    assert product.name in content


def test_creator_application_submission(client, user):
    client.force_login(user)
    res = client.post(
        reverse("creator:creator_apply"),
        {
            "requested_display_name": "Studio Noir",
            "application_bio": "Focusing on dark avant-garde streetwear.",
            "portfolio_url": "https://example.com/lookbook",
        },
    )
    assert res.status_code == 200
    assert "Application Received" in res.content.decode()

    app = CreatorApplication.objects.get(applicant=user)
    assert app.requested_display_name == "Studio Noir"
    assert app.status == CreatorStatus.PENDING


# -----------------------------------------------------------------------------
# 7. Homepage Merchandising Integration
# -----------------------------------------------------------------------------


def test_homepage_creator_section_integration(client, published_post):
    # Create an enabled HomepageSection of type 'creator'
    HomepageSection.objects.create(
        section_type=HomepageSection.SectionType.CREATOR,
        title="Community Styling Spotlight",
        subtitle="Styled by real creators and members",
        is_enabled=True,
        display_order=5,
    )

    res = client.get(reverse("core:home"))
    assert res.status_code == 200
    content = res.content.decode()

    assert "Community Styling Spotlight" in content
    assert published_post.title in content
    assert published_post.creator.display_name in content
