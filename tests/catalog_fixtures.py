"""Catalogue fixtures.

Every fixture here is built with explicit, minimal data so a test that fails names the thing that
broke. The alternative -- a shared "full catalogue" fixture -- tends to make tests depend on
irrelevant rows: a variant count assertion that only passes because the fixture happened to seed
eleven sizes is worse than no assertion.

The factories take overrides through ``**kwargs`` so a test can change exactly the one field it is
about and leave everything else alone.
"""

from decimal import Decimal

import pytest

from apps.catalog.slugs import slug_candidate as apps_slug


@pytest.fixture
def colour(db):
    from apps.catalog.models import Color

    return Color.objects.create(name="Black", slug="black", hex_code="#111827", display_order=10)


@pytest.fixture
def other_colour(db):
    from apps.catalog.models import Color

    return Color.objects.create(name="Sand", slug="sand", hex_code="#e7d8c9", display_order=20)


@pytest.fixture
def inactive_colour(db):
    from apps.catalog.models import Color

    return Color.objects.create(
        name="Discontinued", slug="discontinued", hex_code="#9ca3af", is_active=False
    )


@pytest.fixture
def size(db):
    from apps.catalog.models import Size

    return Size.objects.create(
        name="Medium",
        slug="medium",
        code="M",
        size_type=Size.SizeType.CLOTHING,
        display_order=30,
    )


@pytest.fixture
def other_size(db):
    from apps.catalog.models import Size

    return Size.objects.create(
        name="Large",
        slug="large",
        code="L",
        size_type=Size.SizeType.CLOTHING,
        display_order=40,
    )


@pytest.fixture
def third_colour(db):
    from apps.catalog.models import Color

    return Color.objects.create(name="Olive", slug="olive", hex_code="#4d5d43", display_order=30)


@pytest.fixture
def third_size(db):
    from apps.catalog.models import Size

    return Size.objects.create(
        name="Extra Large",
        slug="extra-large",
        code="XL",
        size_type=Size.SizeType.CLOTHING,
        display_order=50,
    )


@pytest.fixture
def waist_size(db):
    from apps.catalog.models import Size

    return Size.objects.create(name="W32", slug="w32", code="32", size_type=Size.SizeType.NUMERIC)


@pytest.fixture
def material(db):
    from apps.catalog.models import Material

    return Material.objects.create(
        name="Organic cotton", slug="organic-cotton", description="Soft, breathable jersey."
    )


@pytest.fixture
def fit(db):
    from apps.catalog.models import Fit

    return Fit.objects.create(name="Oversized", slug="oversized", description="Roomy.")


@pytest.fixture
def tag(db):
    from apps.catalog.models import ProductTag

    return ProductTag.objects.create(name="New in", slug="new-in")


@pytest.fixture
def department(db):
    from apps.catalog.models import Category

    return Category.objects.create(
        name="Men", slug="men", description="Everything for men.", display_order=10
    )


@pytest.fixture
def category(db, department):
    """A leaf category: products belong to leaves, not to departments."""
    from apps.catalog.models import Category

    return Category.objects.create(
        name="T-Shirts",
        slug="t-shirts",
        parent=department,
        description="Heavyweight cotton tees.",
        display_order=10,
    )


@pytest.fixture
def other_category(db, department):
    from apps.catalog.models import Category

    return Category.objects.create(
        name="Hoodies", slug="hoodies", parent=department, display_order=20
    )


@pytest.fixture
def inactive_category(db, department):
    from apps.catalog.models import Category

    return Category.objects.create(
        name="Archive", slug="archive", parent=department, is_active=False
    )


@pytest.fixture
def brand(db):
    from apps.catalog.models import Brand

    return Brand.objects.create(name="FLASHWEAR", slug="flashwear")


@pytest.fixture
def other_brand(db):
    from apps.catalog.models import Brand

    return Brand.objects.create(name="Northline", slug="northline")


@pytest.fixture
def collection(db):
    from apps.catalog.models import Collection

    return Collection.objects.create(
        name="Core Essentials", slug="core-essentials", description="Never discontinued."
    )


@pytest.fixture
def make_product(db, category, brand):
    """Factory for products. Defaults to a published, priced product."""
    from django.utils import timezone

    from apps.catalog.models import Product

    def _make(name="Heavyweight Tee", **kwargs):
        # ``slug=None`` means "derive it"; ``slug=""`` is passed through on purpose, because a
        # blank slug is exactly what the model's slug resolution is supposed to handle.
        slug = kwargs.pop("slug", None)
        if slug is None:
            slug = apps_slug(name)
        params = {
            "name": name,
            "slug": slug,
            "short_description": "A boxy 240gsm cotton tee.",
            "description": "Pre-shrunk and garment-dyed.",
            "category": category,
            "brand": brand,
            "status": Product.Status.ACTIVE,
            "published_at": timezone.now(),
        }
        params.update(kwargs)
        return Product.objects.create(**params)

    return _make


@pytest.fixture
def product(make_product, colour, size):
    """A live product with one Black / Medium variant priced 49.00.

    The variant carries real options rather than ``(None, None)`` on purpose: the database allows
    exactly one option-less variant per product, so leaving it that way would make every test that
    adds a second variant fail on the wrong constraint.
    """
    from apps.catalog.models import ProductVariant

    product = make_product()
    ProductVariant.objects.create(
        product=product, sku="FW-TEE-BLK-M", color=colour, size=size, price=Decimal("49.00")
    )
    return product


@pytest.fixture
def make_variant(db):
    """Factory for variants, generating a SKU when none is supplied."""
    from apps.catalog.models import ProductVariant
    from apps.catalog.skus import build_sku

    def _make(product, *, color=None, size=None, price=Decimal("49.00"), **kwargs):
        params = {
            "product": product,
            "sku": kwargs.pop("sku", None) or build_sku(product=product, color=color, size=size),
            "color": color,
            "size": size,
            "price": price,
        }
        params.update(kwargs)
        return ProductVariant.objects.create(**params)

    return _make


@pytest.fixture
def full_product(
    make_product,
    colour,
    other_colour,
    size,
    other_size,
    make_variant,
    material,
    fit,
    tag,
    collection,
):
    """A product with two colours x two sizes, materials, fit, tags and a collection.

    This is the shape most storefront tests need: enough to exercise selection, sale pricing and
    merchandising links without any test having to assemble it.
    """
    product = make_product(
        name="Full T-Shirt",
        slug="full-t-shirt",
        short_description="Two colours, two sizes.",
        description="Everything attached.",
        fit=fit,
        is_featured=True,
    )
    product.materials.set([material])
    product.tags.set([tag])
    product.collections.set([collection])

    for colour_option in (colour, other_colour):
        for size_option in (size, other_size):
            make_variant(
                product,
                color=colour_option,
                size=size_option,
                price=Decimal("49.00"),
                compare_at_price=Decimal("69.00") if colour_option == colour else None,
            )
    return product


@pytest.fixture
def draft_product(make_product):
    from apps.catalog.models import Product

    return make_product(name="Unfinished Tee", slug="unfinished-tee", status=Product.Status.DRAFT)


@pytest.fixture
def archived_product(make_product):
    from django.utils import timezone

    from apps.catalog.models import Product

    return make_product(
        name="Retired Tee",
        slug="retired-tee",
        status=Product.Status.ARCHIVED,
        published_at=timezone.now() - timezone.timedelta(days=30),
    )


@pytest.fixture
def scheduled_product(make_product):
    """Active, but dated in the future: not yet public."""
    from django.utils import timezone

    return make_product(
        name="Embargoed Drop",
        slug="embargoed-drop",
        published_at=timezone.now() + timezone.timedelta(days=7),
    )
