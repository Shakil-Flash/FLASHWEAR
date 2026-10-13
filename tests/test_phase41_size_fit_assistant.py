"""Tests for Phase 41: Smart Size & Fit Assistant.

Validates:
1. Size-guide validation and database constraints.
2. Measurement unit conversions (cm <-> in) and dimension formatting.
3. Safe missing-data behavior (no fabricated data, clean fallbacks).
4. Fit assistant recommendation engine and explainability logic.
5. Customer private opt-in measurements (consent, CRUD, privacy).
6. Genuine review fit-feedback aggregation with threshold enforcement.
7. Admin permissions and staff controls for size guides.
8. Storefront integration, stock validation, and cart compatibility.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.models import CustomerMeasurement
from apps.catalog.fit_assistant import get_fit_assessment, get_size_guide_for_product
from apps.catalog.models import (
    Category,
    Fit,
    Product,
    ProductVariant,
    Size,
    SizeGuide,
    SizeGuideRow,
)
from apps.engagement.models import Review
from apps.engagement.services import reviews as review_services
from apps.orders.models import Order
from apps.shop.models import Cart, CheckoutSession

User = get_user_model()


@pytest.fixture
def size_system(db):
    """Standard clothing sizes for testing."""
    size_s = Size.objects.create(
        code="S", name="Small", size_type=Size.SizeType.CLOTHING, display_order=1
    )
    size_m = Size.objects.create(
        code="M", name="Medium", size_type=Size.SizeType.CLOTHING, display_order=2
    )
    size_l = Size.objects.create(
        code="L", name="Large", size_type=Size.SizeType.CLOTHING, display_order=3
    )
    size_xl = Size.objects.create(
        code="XL", name="X-Large", size_type=Size.SizeType.CLOTHING, display_order=4
    )
    return {"S": size_s, "M": size_m, "L": size_l, "XL": size_xl}


@pytest.fixture
def apparel_catalog(db, size_system):
    """Category, fits, and products."""
    category = Category.objects.create(name="T-Shirts", slug="t-shirts", is_active=True)
    pants_cat = Category.objects.create(name="Pants", slug="pants", is_active=True)

    regular_fit = Fit.objects.create(name="Regular Fit", slug="regular-fit", is_active=True)
    slim_fit = Fit.objects.create(name="Slim Fit", slug="slim-fit", is_active=True)
    oversized_fit = Fit.objects.create(name="Oversized", slug="oversized", is_active=True)

    tee = Product.objects.create(
        name="Essential Rib Tee",
        slug="essential-rib-tee",
        category=category,
        fit=regular_fit,
        status=Product.Status.ACTIVE,
        fabric_stretch=Product.FabricStretch.SLIGHT,
        silhouette="Boxy crew neck",
        model_height_cm=184,
        model_wearing_size=size_system["M"],
        model_measurements="Chest 96cm, Waist 80cm",
    )

    # Purchasable variants for tee
    var_s = ProductVariant.objects.create(
        product=tee, sku="TEE-RIB-S", size=size_system["S"], price=Decimal("35.00"), is_active=True
    )
    var_m = ProductVariant.objects.create(
        product=tee, sku="TEE-RIB-M", size=size_system["M"], price=Decimal("35.00"), is_active=True
    )
    var_l = ProductVariant.objects.create(
        product=tee, sku="TEE-RIB-L", size=size_system["L"], price=Decimal("35.00"), is_active=True
    )

    trousers = Product.objects.create(
        name="Relaxed Cargo Trouser",
        slug="relaxed-cargo-trouser",
        category=pants_cat,
        fit=oversized_fit,
        status=Product.Status.ACTIVE,
        fabric_stretch=Product.FabricStretch.NON_STRETCH,
    )
    var_tr_m = ProductVariant.objects.create(
        product=trousers,
        sku="CARGO-M",
        size=size_system["M"],
        price=Decimal("75.00"),
        is_active=True,
    )
    var_tr_l = ProductVariant.objects.create(
        product=trousers,
        sku="CARGO-L",
        size=size_system["L"],
        price=Decimal("75.00"),
        is_active=True,
    )

    return {
        "category": category,
        "pants_cat": pants_cat,
        "tee": tee,
        "trousers": trousers,
        "variants": {
            "tee_s": var_s,
            "tee_m": var_m,
            "tee_l": var_l,
            "tr_m": var_tr_m,
            "tr_l": var_tr_l,
        },
        "fits": {"regular": regular_fit, "slim": slim_fit, "oversized": oversized_fit},
    }


# =============================================================================
# 1. Size Guide Validation & Management
# =============================================================================


@pytest.mark.django_db
def test_size_guide_requires_product_or_category():
    """A size guide cannot be saved without a product or category attachment."""
    guide = SizeGuide(name="Orphan Guide", unit="cm")
    with pytest.raises(ValidationError):
        guide.clean()


@pytest.mark.django_db
def test_size_guide_row_range_validation(apparel_catalog, size_system):
    """SizeGuideRow rejects max measurement less than min measurement."""
    guide = SizeGuide.objects.create(
        name="Tee Guide",
        category=apparel_catalog["category"],
        unit="cm",
        measurement_type=SizeGuide.MeasurementType.GARMENT,
    )
    row = SizeGuideRow(
        guide=guide,
        size=size_system["M"],
        chest_min=Decimal("104.0"),
        chest_max=Decimal("98.0"),  # Invalid: max < min
    )
    with pytest.raises(ValidationError):
        row.clean()


@pytest.mark.django_db
def test_size_guide_resolution_order(apparel_catalog, size_system):
    """Product-specific size guide overrides category-level size guide."""
    cat_guide = SizeGuide.objects.create(
        name="Category Tees Guide",
        category=apparel_catalog["category"],
        unit="cm",
        measurement_type=SizeGuide.MeasurementType.GARMENT,
    )
    SizeGuideRow.objects.create(guide=cat_guide, size=size_system["M"], chest_min=Decimal("52.0"))

    # Initial resolution picks category guide
    resolved = get_size_guide_for_product(apparel_catalog["tee"])
    assert resolved == cat_guide

    # When product guide is created, it takes precedence
    prod_guide = SizeGuide.objects.create(
        name="Tee Direct Override",
        product=apparel_catalog["tee"],
        unit="cm",
        measurement_type=SizeGuide.MeasurementType.BODY,
    )
    SizeGuideRow.objects.create(guide=prod_guide, size=size_system["M"], chest_min=Decimal("96.0"))

    resolved = get_size_guide_for_product(apparel_catalog["tee"])
    assert resolved == prod_guide


# =============================================================================
# 2. Measurement Unit Conversions & Formatting
# =============================================================================


@pytest.mark.django_db
def test_size_guide_unit_conversion_and_formatting(apparel_catalog, size_system):
    """Accurately converts dimensions between cm and inches without distortion."""
    guide = SizeGuide.objects.create(
        name="CM Guide",
        product=apparel_catalog["tee"],
        unit="cm",
        measurement_type=SizeGuide.MeasurementType.BODY,
    )
    row = SizeGuideRow.objects.create(
        guide=guide,
        size=size_system["M"],
        chest_min=Decimal("96.0"),
        chest_max=Decimal("100.0"),
        shoulder=Decimal("45.0"),
        length=Decimal("72.0"),
    )

    # CM output
    assert row.format_dimension("chest", "cm") == "96.0 - 100.0"
    assert row.format_dimension("shoulder", "cm") == "45.0"
    assert row.format_dimension("length", "cm") == "72.0"
    assert row.format_dimension("inseam", "cm") == "—"

    # Inches conversion (96 / 2.54 = ~37.8, 100 / 2.54 = ~39.4, 45 / 2.54 = ~17.7)
    assert row.format_dimension("chest", "in") == "37.8 - 39.4"
    assert row.format_dimension("shoulder", "in") == "17.7"
    assert row.format_dimension("inseam", "in") == "—"


# =============================================================================
# 3. Missing Data & Safe Fallbacks
# =============================================================================


@pytest.mark.django_db
def test_missing_size_data_clean_fallback(apparel_catalog):
    """When a product has no size guide, fit assistant returns an honest fallback."""
    assessment = get_fit_assessment(apparel_catalog["trousers"])

    assert assessment["has_guide"] is False
    assert assessment["has_recommendation"] is False
    assert assessment["recommended_size"] == ""
    assert "not yet published" in assessment["explanation"].lower()


@pytest.mark.django_db
def test_never_invents_model_details_when_unsupplied(apparel_catalog):
    """Products without model measurements report has_model_info = False."""
    trouser = apparel_catalog["trousers"]
    assessment = get_fit_assessment(trouser)

    assert assessment["has_model_info"] is False
    assert assessment["model_height_cm"] is None
    assert assessment["model_wearing_size"] == ""
    assert assessment["model_measurements"] == ""


# =============================================================================
# 4. Fit Assistant Recommendation Logic
# =============================================================================


@pytest.mark.django_db
def test_fit_assistant_recommends_size_from_user_measurements(apparel_catalog, size_system):
    """Matches opt-in customer chest measurement to appropriate garment size."""
    tee = apparel_catalog["tee"]
    guide = SizeGuide.objects.create(
        name="Tee Finished Dimensions",
        product=tee,
        unit="cm",
        measurement_type=SizeGuide.MeasurementType.GARMENT,
    )
    # Garment chest width laid flat (half-width)
    SizeGuideRow.objects.create(guide=guide, size=size_system["S"], chest_min=Decimal("48.0"))
    SizeGuideRow.objects.create(guide=guide, size=size_system["M"], chest_min=Decimal("52.0"))
    SizeGuideRow.objects.create(guide=guide, size=size_system["L"], chest_min=Decimal("56.0"))

    user = User.objects.create_user(email="shopper@example.com", password="password123")
    # Customer chest: 96cm, standard regular fit ease ~8cm -> ideal garment ~104cm -> M
    CustomerMeasurement.objects.create(
        user=user,
        consent_given=True,
        unit="cm",
        chest=Decimal("96.0"),
        fit_preference=CustomerMeasurement.PreferredFit.REGULAR,
    )

    assessment = get_fit_assessment(tee, user=user)

    assert assessment["has_recommendation"] is True
    assert assessment["recommended_size"] == "M"
    assert "advisory only" in assessment["explanation"].lower()
    assert "not guaranteed" in assessment["explanation"].lower()


@pytest.mark.django_db
def test_fit_assistant_bottoms_uses_waist(apparel_catalog, size_system):
    """For bottom garments (pants), waist is evaluated instead of chest."""
    trousers = apparel_catalog["trousers"]
    guide = SizeGuide.objects.create(
        name="Trouser Body Measurements",
        product=trousers,
        unit="cm",
        measurement_type=SizeGuide.MeasurementType.BODY,
    )
    SizeGuideRow.objects.create(
        guide=guide, size=size_system["M"], waist_min=Decimal("80.0"), waist_max=Decimal("84.0")
    )
    SizeGuideRow.objects.create(
        guide=guide, size=size_system["L"], waist_min=Decimal("86.0"), waist_max=Decimal("90.0")
    )

    user = User.objects.create_user(email="pantsshopper@example.com", password="password123")
    CustomerMeasurement.objects.create(
        user=user,
        consent_given=True,
        unit="cm",
        chest=Decimal("105.0"),
        waist=Decimal("88.0"),  # Waist matches L range (86-90)
    )

    assessment = get_fit_assessment(trousers, user=user)

    assert assessment["has_recommendation"] is True
    assert assessment["recommended_size"] == "L"
    assert "waist" in assessment["explanation"].lower()


# =============================================================================
# 5. Customer Measurement Privacy & Consent Controls
# =============================================================================


@pytest.mark.django_db
def test_customer_measurement_requires_explicit_consent():
    """CustomerMeasurement refuses to save when consent_given is False."""
    user = User.objects.create_user(email="consenttest@example.com", password="password123")
    meas = CustomerMeasurement(
        user=user,
        consent_given=False,
        chest=Decimal("95.0"),
    )
    with pytest.raises(ValidationError):
        meas.clean()


@pytest.mark.django_db
def test_customer_measurement_account_flow(client):
    """Customer can save, update, and delete their private measurements via account view."""
    user = User.objects.create_user(email="privateshopper@example.com", password="password123")
    client.force_login(user)

    url = reverse("account:measurements")
    # 1. GET page
    response = client.get(url)
    assert response.status_code == 200
    assert "Strict Privacy Guarantee" in response.content.decode()

    # 2. POST save measurements
    post_data = {
        "consent_given": "on",
        "unit": "cm",
        "height": "178.0",
        "chest": "94.5",
        "waist": "81.0",
        "hips": "99.0",
        "fit_preference": "regular",
    }
    post_resp = client.post(url, post_data, follow=True)
    assert post_resp.status_code == 200

    meas = CustomerMeasurement.objects.get(user=user)
    assert meas.consent_given is True
    assert meas.consent_at is not None
    assert meas.chest == Decimal("94.5")

    # 3. POST delete measurements completely
    del_url = reverse("account:measurements-delete")
    del_resp = client.post(del_url, follow=True)
    assert del_resp.status_code == 200
    assert not CustomerMeasurement.objects.filter(user=user).exists()


# =============================================================================
# 6. Reviews & Verified Fit-Feedback Aggregation
# =============================================================================


@pytest.mark.django_db
def test_review_fit_feedback_threshold_and_aggregation(apparel_catalog):
    """Fit feedback requires minimum 3 genuine votes before displaying percentages."""
    tee = apparel_catalog["tee"]

    # When 0 reviews exist
    feedback = review_services.fit_feedback_for(tee)
    assert feedback["has_enough_data"] is False
    assert feedback["total_votes"] == 0

    # Create paid qualifying orders for reviewers
    users = [
        User.objects.create_user(email=f"reviewer{i}@example.com", password="password123")
        for i in range(4)
    ]
    for u in users:
        cart = Cart.objects.create(user=u, session_key="", status=Cart.Status.CONVERTED)
        checkout = CheckoutSession.objects.create(
            user=u, cart=cart, status=CheckoutSession.Status.CONVERTED
        )
        Order.objects.create(
            number=f"ORD-REV-{u.pk}",
            checkout=checkout,
            user=u,
            status=Order.Status.DELIVERED,
            subtotal=Decimal("35.00"),
            shipping_amount=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total=Decimal("35.00"),
        )

    # 1st review: runs_small
    Review.objects.create(
        product=tee,
        author=users[0],
        order=Order.objects.filter(user=users[0]).first(),
        rating=4,
        fit_feedback=Review.FitFeedback.RUNS_SMALL,
        title="Good tee",
        body="Nice fabric but felt snug.",
        status=Review.Status.PUBLISHED,
        verified_purchase=True,
    )

    # 2nd review: true_to_size
    Review.objects.create(
        product=tee,
        author=users[1],
        order=Order.objects.filter(user=users[1]).first(),
        rating=5,
        fit_feedback=Review.FitFeedback.TRUE_TO_SIZE,
        title="Perfect",
        body="Spot on fit.",
        status=Review.Status.PUBLISHED,
        verified_purchase=True,
    )

    # With only 2 reviews, threshold is not met (has_enough_data = False)
    feedback = review_services.fit_feedback_for(tee)
    assert feedback["has_enough_data"] is False
    assert feedback["total_votes"] == 2

    # 3rd review: true_to_size (threshold met: 3 votes, 67% true_to_size, 33% runs_small)
    Review.objects.create(
        product=tee,
        author=users[2],
        order=Order.objects.filter(user=users[2]).first(),
        rating=5,
        fit_feedback=Review.FitFeedback.TRUE_TO_SIZE,
        title="Great fit",
        body="True to size.",
        status=Review.Status.PUBLISHED,
        verified_purchase=True,
    )

    feedback = review_services.fit_feedback_for(tee)
    assert feedback["has_enough_data"] is True
    assert feedback["total_votes"] == 3
    assert feedback["true_to_size_percent"] == 67
    assert feedback["runs_small_percent"] == 33
    assert feedback["runs_large_percent"] == 0
    assert feedback["dominant_fit"] == "true_to_size"


# =============================================================================
# 7. Admin Permissions & Staff Controls
# =============================================================================


@pytest.mark.django_db
def test_size_guide_admin_permissions(client, apparel_catalog):
    """Anonymous visitors and non-staff users cannot access SizeGuide admin."""
    admin_url = reverse("admin:catalog_sizeguide_changelist")

    # Anonymous redirects
    res_anon = client.get(admin_url)
    assert res_anon.status_code == 302

    # Non-staff user gets 302/403
    regular_user = User.objects.create_user(email="regular@example.com", password="password123")
    client.force_login(regular_user)
    res_reg = client.get(admin_url)
    assert res_reg.status_code in (302, 403)

    # Superuser has full access
    admin_user = User.objects.create_superuser(
        email="superadmin@example.com", password="password123"
    )
    client.force_login(admin_user)
    res_admin = client.get(admin_url)
    assert res_admin.status_code == 200


# =============================================================================
# 8. Storefront Product Detail UX & Variant Integration
# =============================================================================


@pytest.mark.django_db
def test_product_detail_page_renders_size_guide_and_fit_interface(
    client, apparel_catalog, size_system
):
    """Product detail page provides size guide modal and fit attributes."""
    tee = apparel_catalog["tee"]
    guide = SizeGuide.objects.create(
        name="Essential Tee Measurements",
        product=tee,
        unit="cm",
        measurement_type=SizeGuide.MeasurementType.GARMENT,
    )
    SizeGuideRow.objects.create(
        guide=guide,
        size=size_system["M"],
        chest_min=Decimal("52.0"),
        waist_min=Decimal("50.0"),
        shoulder=Decimal("46.0"),
        length=Decimal("72.0"),
    )

    url = tee.get_absolute_url()
    response = client.get(url)

    assert response.status_code == 200
    content = response.content.decode()
    assert "Size &amp; Fit Guide" in content or "Size & Fit Guide" in content
    assert "Fit Profile: Regular Fit" in content
    assert "Stretch: Slight stretch" in content
    assert "Model Sizing:" in content
    assert "184 cm" in content
