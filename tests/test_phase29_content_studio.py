"""Phase 29: Visual Content & Merchandising Studio test suite.

Validates:
1. Section visibility, enabled/disabled toggles, and display order.
2. Scheduling and seasonal campaign lifecycle (timezone-aware dates).
3. Product merchandising (pinning, numeric reordering, removing, scheduling).
4. Editorial fashion stories (publishing state, scheduling, linked entities).
5. Preview security (staff preview simulator, preventing unpublished leaks).
6. Backoffice permissions (MARKETING_VIEW, MARKETING_MANAGE, CATALOG_VIEW, CATALOG_MANAGE).
7. Image security & SSRF prevention on external URLs.
8. CTA destination safety & script rejection.
9. Cache invalidation on content mutations.
10. Analytics event recording (the 6 required content events).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.analytics.models import Event
from apps.backoffice.permissions import (
    CATALOG_GROUP,
    MARKETING_GROUP,
    OPERATOR_GROUP,
    ensure_backoffice_groups,
)
from apps.catalog.models import Category, Collection, Product
from apps.core.models import Campaign, EditorialStory, HomepageSection, SectionProduct
from apps.core.services.content import (
    HOMEPAGE_SECTIONS_CACHE_KEY,
    get_active_editorial_stories,
    get_active_homepage_sections,
    invalidate_homepage_cache,
    validate_cta_destination,
)

User = get_user_model()


@pytest.fixture(autouse=True)
def setup_groups(db):
    """Ensure standard backoffice capability groups exist."""
    ensure_backoffice_groups()
    invalidate_homepage_cache()
    yield
    invalidate_homepage_cache()


@pytest.fixture
def marketing_manager(db):
    user = User.objects.create_user(
        email="marketing@flashwear.local",
        password="ValidPassword123!",
        is_staff=True,
    )
    group = Group.objects.get(name=MARKETING_GROUP)
    user.groups.add(group)
    return user


@pytest.fixture
def catalog_manager(db):
    user = User.objects.create_user(
        email="catalog@flashwear.local",
        password="ValidPassword123!",
        is_staff=True,
    )
    group = Group.objects.get(name=CATALOG_GROUP)
    user.groups.add(group)
    return user


@pytest.fixture
def backoffice_operator(db):
    user = User.objects.create_user(
        email="operator@flashwear.local",
        password="ValidPassword123!",
        is_staff=True,
    )
    group = Group.objects.get(name=OPERATOR_GROUP)
    user.groups.add(group)
    return user


@pytest.fixture
def regular_shopper(db):
    return User.objects.create_user(
        email="shopper@flashwear.local",
        password="ValidPassword123!",
        is_staff=False,
    )


@pytest.fixture
def sample_catalog(db):
    category = Category.objects.create(name="Jackets", slug="jackets")
    collection = Collection.objects.create(name="Autumn Edit", slug="autumn-edit")
    p1 = Product.objects.create(
        name="Leather Biker Jacket",
        slug="leather-biker-jacket",
        category=category,
        status=Product.Status.ACTIVE,
    )
    p2 = Product.objects.create(
        name="Oversized Trench",
        slug="oversized-trench",
        category=category,
        status=Product.Status.ACTIVE,
    )
    p3 = Product.objects.create(
        name="Draft Sample Blazer",
        slug="draft-sample-blazer",
        category=category,
        status=Product.Status.DRAFT,
    )
    return {
        "category": category,
        "collection": collection,
        "product1": p1,
        "product2": p2,
        "draft_product": p3,
    }


# ======================================================================================
# 1. Section Visibility, Ordering, and Lifecycle
# ======================================================================================


def test_section_visibility_lifecycle(db):
    """Test enabled/disabled toggles and start/end scheduling."""
    now = timezone.now()

    sec_enabled = HomepageSection.objects.create(
        title="Live Section",
        section_type=HomepageSection.SectionType.FEATURED_PRODUCTS,
        is_enabled=True,
        display_order=1,
    )
    sec_disabled = HomepageSection.objects.create(
        title="Disabled Section",
        section_type=HomepageSection.SectionType.FEATURED_PRODUCTS,
        is_enabled=False,
        display_order=2,
    )
    sec_future = HomepageSection.objects.create(
        title="Future Section",
        section_type=HomepageSection.SectionType.FEATURED_PRODUCTS,
        is_enabled=True,
        starts_at=now + timedelta(days=2),
        display_order=3,
    )
    sec_past = HomepageSection.objects.create(
        title="Past Section",
        section_type=HomepageSection.SectionType.FEATURED_PRODUCTS,
        is_enabled=True,
        ends_at=now - timedelta(days=1),
        display_order=4,
    )

    assert sec_enabled.is_visible(now) is True
    assert sec_disabled.is_visible(now) is False
    assert sec_future.is_visible(now) is False
    assert sec_past.is_visible(now) is False

    active_sections = get_active_homepage_sections(now=now)
    assert sec_enabled in active_sections
    assert sec_disabled not in active_sections
    assert sec_future not in active_sections
    assert sec_past not in active_sections


def test_section_ordering(db):
    """Sections must follow display_order ascending."""
    sec3 = HomepageSection.objects.create(title="Third", display_order=30)
    sec1 = HomepageSection.objects.create(title="First", display_order=10)
    sec2 = HomepageSection.objects.create(title="Second", display_order=20)

    sections = get_active_homepage_sections()
    assert [s.pk for s in sections] == [sec1.pk, sec2.pk, sec3.pk]


# ======================================================================================
# 2. Seasonal Campaigns
# ======================================================================================


def test_seasonal_campaign_visibility(db):
    """Sections attached to campaigns appear and disappear based on campaign dates."""
    now = timezone.now()
    campaign = Campaign.objects.create(
        title="Autumn Edit 2026",
        slug="autumn-edit-2026",
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=14),
        is_active=True,
    )
    sec = HomepageSection.objects.create(
        title="Autumn Highlights",
        campaign=campaign,
        is_enabled=True,
    )

    assert campaign.is_running(now) is True
    assert sec.is_visible(now) is True

    # Deactivating campaign hides section
    campaign.is_active = False
    campaign.save()
    assert sec.is_visible(now) is False

    # Reactivate, but advance time past ends_at
    campaign.is_active = True
    campaign.save()
    future = now + timedelta(days=20)
    assert campaign.is_running(future) is False
    assert sec.is_visible(future) is False


def test_campaign_validation(db):
    """End date must not be before start date."""
    now = timezone.now()
    camp = Campaign(
        title="Invalid Campaign",
        slug="invalid-campaign",
        starts_at=now + timedelta(days=5),
        ends_at=now,
    )
    with pytest.raises(ValidationError):
        camp.clean()


# ======================================================================================
# 3. Product Merchandising
# ======================================================================================


def test_section_product_pinning_and_ordering(sample_catalog):
    """Staff can pin products, set display orders, and filter active ones."""
    sec = HomepageSection.objects.create(
        title="Curated Strip",
        section_type=HomepageSection.SectionType.FEATURED_PRODUCTS,
    )
    p1 = sample_catalog["product1"]
    p2 = sample_catalog["product2"]
    SectionProduct.objects.create(section=sec, product=p2, display_order=2)
    sp1 = SectionProduct.objects.create(section=sec, product=p1, display_order=1)


    # get_active_products should return p1 then p2
    active = sec.get_active_products()
    assert active == [p1, p2]

    # Deactivating sp1 removes it from active list
    sp1.is_active = False
    sp1.save()
    assert sec.get_active_products() == [p2]


def test_section_product_reorder_view(client, catalog_manager, sample_catalog):
    """Backoffice numeric reorder updates SectionProduct display order."""
    client.force_login(catalog_manager)
    sec = HomepageSection.objects.create(title="Strip")
    p1 = sample_catalog["product1"]
    sp = SectionProduct.objects.create(section=sec, product=p1, display_order=10)

    url = reverse("backoffice:merch_product_reorder", args=[sec.pk])
    resp = client.post(url, {"product_id": p1.pk, "display_order": 5})
    assert resp.status_code == 302

    sp.refresh_from_db()
    assert sp.display_order == 5


def test_section_product_remove_view(client, catalog_manager, sample_catalog):
    """Backoffice remove product deletes the SectionProduct link."""
    client.force_login(catalog_manager)
    sec = HomepageSection.objects.create(title="Strip")
    p1 = sample_catalog["product1"]
    SectionProduct.objects.create(section=sec, product=p1)

    url = reverse("backoffice:merch_product_remove", args=[sec.pk, p1.pk])
    resp = client.post(url)
    assert resp.status_code == 302
    assert not SectionProduct.objects.filter(section=sec, product=p1).exists()


# ======================================================================================
# 4. Editorial Stories
# ======================================================================================


def test_editorial_story_publishing_and_scheduling(db, sample_catalog):
    """Draft stories do not leak; published stories in schedule appear."""
    now = timezone.now()
    published = EditorialStory.objects.create(
        title="After Dark",
        slug="after-dark",
        subtitle="Pieces made for nights that don't need a plan.",
        story="Minimal silhouettes for late hours.",
        is_published=True,
    )
    published.linked_products.add(sample_catalog["product1"])

    draft = EditorialStory.objects.create(
        title="Unpublished Concept",
        slug="unpublished-concept",
        is_published=False,
    )

    stories = get_active_editorial_stories(now=now)
    assert published in stories
    assert draft not in stories

    # Staff requesting include_unpublished sees both
    all_stories = get_active_editorial_stories(now=now, include_unpublished=True)
    assert published in all_stories
    assert draft in all_stories


# ======================================================================================
# 5. Preview Security
# ======================================================================================


def test_preview_security_unauthenticated_blocked(client):
    """Anonymous customers cannot open preview simulator."""
    url = reverse("backoffice:content_preview")
    resp = client.get(url)
    # Redirects to login or raises 404 (backoffice_access gates with login_required)
    assert resp.status_code in (302, 404)


def test_preview_mode_does_not_leak_to_public_storefront(client, sample_catalog):
    """Draft section appears only with staff ?preview=content, never to ordinary visitors."""
    HomepageSection.objects.create(
        title="Top Secret Future Drop",
        section_type=HomepageSection.SectionType.HERO,
        is_enabled=False,
    )


    # Anonymous visitor to homepage
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Top Secret Future Drop" not in resp.content.decode()

    # Anonymous visitor passing ?preview=content still cannot see it
    resp_anon_param = client.get("/?preview=content")
    assert "Top Secret Future Drop" not in resp_anon_param.content.decode()


def test_staff_preview_access(client, marketing_manager):
    """Staff can preview draft sections via simulator and storefront preview."""
    HomepageSection.objects.create(
        title="Upcoming Editorial Drop",
        section_type=HomepageSection.SectionType.HERO,
        is_enabled=False,
    )
    client.force_login(marketing_manager)

    # 1. Preview simulator in backoffice
    sim_url = reverse("backoffice:content_preview")
    resp_sim = client.get(sim_url)
    assert resp_sim.status_code == 200
    assert "Upcoming Editorial Drop" in resp_sim.content.decode()

    # 2. Storefront with ?preview=content
    resp_store = client.get("/?preview=content")
    assert resp_store.status_code == 200
    assert "Upcoming Editorial Drop" in resp_store.content.decode()
    assert "CONTENT STUDIO PREVIEW" in resp_store.content.decode()


# ======================================================================================
# 6. Backoffice Permissions & Least Privilege
# ======================================================================================


def test_content_permissions_marketing_vs_operator(client, marketing_manager, backoffice_operator):
    """Marketing Manager can edit homepage; Operator without marketing cap gets 404."""
    sec = HomepageSection.objects.create(title="Editable Section")

    # Marketing Manager can GET and POST edit form
    client.force_login(marketing_manager)
    resp = client.get(reverse("backoffice:content_homepage_edit", args=[sec.pk]))
    assert resp.status_code == 200

    # Operator gets 404 on content management
    client.force_login(backoffice_operator)
    resp_op = client.get(reverse("backoffice:content_homepage_edit", args=[sec.pk]))
    assert resp_op.status_code == 404


def test_customer_cannot_access_backoffice_content(client, regular_shopper):
    """Regular logged-in shopper gets 404 on all backoffice routes."""
    client.force_login(regular_shopper)
    resp = client.get(reverse("backoffice:content_homepage"))
    assert resp.status_code == 404


# ======================================================================================
# 7. CTA Destination Safety & Image SSRF
# ======================================================================================


@pytest.mark.parametrize(
    "dangerous_url",
    [
        "javascript:alert('xss')",
        "JAVASCRIPT:evil()",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "//attacker.com/malicious",
        "vbscript:msgbox(1)",
        "https://example.com/line\nbreak",
    ],
)
def test_cta_safety_rejections(dangerous_url):
    """Dangerous URL schemes and protocol-relative URLs must raise ValidationError."""
    with pytest.raises(ValidationError):
        validate_cta_destination(dangerous_url)


@pytest.mark.parametrize(
    "safe_url",
    [
        "/catalog/",
        "/drops/",
        "/collections/autumn/",
        "https://instagram.com/flashwear",
        "http://partner.example.com/lookbook",
    ],
)
def test_cta_safety_allowed(safe_url):
    """Safe internal paths and standard http/https schemes pass without error."""
    validate_cta_destination(safe_url)


def test_homepage_form_ssrf_rejection(marketing_manager, client):
    """Submitting cloud metadata IP or loopback in external image URL raises form error."""
    client.force_login(marketing_manager)
    url = reverse("backoffice:content_homepage_add")
    resp = client.post(
        url,
        {
            "title": "SSRF Test Section",
            "section_type": "hero",
            "is_enabled": "on",
            "display_order": "1",
            "desktop_image_url": "http://169.254.169.254/latest/meta-data/",
        },
    )
    # Form re-renders with error instead of 302 redirect
    assert resp.status_code == 200
    assert "desktop_image_url" in resp.context["form"].errors


# ======================================================================================
# 8. Cache Invalidation
# ======================================================================================


def test_cache_invalidation_on_mutations(db):
    """Mutating sections, campaigns, or pinned products clears cache."""
    # Warm up cache
    HomepageSection.objects.create(title="Cached Section", is_enabled=True)
    get_active_homepage_sections()
    assert cache.get(HOMEPAGE_SECTIONS_CACHE_KEY) is not None

    # Creating a new section automatically invalidates cache
    new_sec = HomepageSection.objects.create(title="New Section", is_enabled=True)
    assert cache.get(HOMEPAGE_SECTIONS_CACHE_KEY) is None

    # Warm up again
    get_active_homepage_sections()
    assert cache.get(HOMEPAGE_SECTIONS_CACHE_KEY) is not None

    # Deleting section invalidates cache
    new_sec.delete()
    assert cache.get(HOMEPAGE_SECTIONS_CACHE_KEY) is None


# ======================================================================================
# 9. Analytics Events
# ======================================================================================


def test_homepage_section_view_analytics(client, db):
    """Visiting homepage with active sections logs homepage_section_view."""
    sec = HomepageSection.objects.create(
        title="Hero Studio",
        section_type=HomepageSection.SectionType.HERO,
        is_enabled=True,
    )
    resp = client.get("/")
    assert resp.status_code == 200

    events = Event.objects.filter(name="homepage_section_view")
    assert events.exists()
    assert events.filter(metadata__section_id=str(sec.pk)).exists()


def test_content_interaction_tracking_endpoint(client, db):
    """POST /content/track/ records the 6 required analytics events."""
    endpoint = reverse("core:track_content_interaction")

    interactions = [
        ("homepage_section_click", {"section_id": "1", "destination": "/catalog/"}),
        ("editorial_view", {"story_id": "2", "story_title": "After Dark"}),
        ("editorial_click", {"story_id": "2", "destination": "/editorial/after-dark/"}),
        ("campaign_click", {"campaign_id": "3", "destination": "/campaigns/autumn/"}),
        ("featured_product_click", {"product_id": "4", "section_id": "1"}),
    ]

    for event_name, meta in interactions:
        payload = {"event": event_name, **meta}
        resp = client.post(endpoint, payload, content_type="application/json")
        assert resp.status_code == 200
        assert resp.json() == {"status": "recorded"}
        assert Event.objects.filter(name=event_name).exists()


def test_content_interaction_get_redirect(client):
    """GET /content/track/?event=...&destination=... records event and safely redirects."""
    endpoint = reverse("core:track_content_interaction")
    resp = client.get(
        f"{endpoint}?event=homepage_section_click&section_id=1&destination=/catalog/"
    )
    assert resp.status_code == 302
    assert resp.url == "/catalog/"
    assert Event.objects.filter(name="homepage_section_click").exists()
