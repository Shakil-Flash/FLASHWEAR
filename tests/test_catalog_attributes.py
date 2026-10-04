"""Tests for the controlled vocabularies: sizes, colours, materials, fits and tags.

Small models, small jobs: normalise what is entered, reject what cannot be rendered, and compute the
colour values the storefront and admin draw with.
"""

import pytest
from django.core.exceptions import ValidationError

from apps.catalog.models import Color, Fit, Material, ProductTag, Size

pytestmark = pytest.mark.django_db


class TestSize:
    def test_str_includes_the_code(self, size):
        """The code is what a shopper reads, the name what a screen reader announces."""
        assert str(size) == "Medium (M)"

    def test_code_is_trimmed_and_upper_cased(self, db):
        row = Size.objects.create(
            name="Extra Large",
            slug="extra-large",
            code="  xl  ",
            size_type=Size.SizeType.CLOTHING,
        )
        assert row.code == "XL"

    def test_a_numeric_size_is_allowed(self, waist_size):
        assert waist_size.size_type == Size.SizeType.NUMERIC

    def test_a_shoe_size_is_allowed(self, db):
        row = Size.objects.create(
            name="UK 8", slug="uk-8", code="UK8", size_type=Size.SizeType.SHOE
        )
        assert row.size_type == Size.SizeType.SHOE

    def test_one_size_is_allowed(self, db):
        row = Size.objects.create(
            name="One size", slug="one-size", code="OS", size_type=Size.SizeType.ONE_SIZE
        )
        assert row.code == "OS"

    def test_display_order_drives_the_ordering(self, db):
        Size.objects.create(name="Large", slug="l2", code="L2", display_order=40)
        Size.objects.create(name="Small", slug="s2", code="S2", display_order=20)
        assert [row.code for row in Size.objects.all()] == ["S2", "L2"]


class TestColor:
    def test_rgb_parses_the_hex(self, colour):
        assert colour.rgb == (17, 24, 39)

    def test_hex_is_upper_cased_on_save(self, db):
        row = Color.objects.create(name="Navy", slug="navy", hex_code="#1e3a5f")
        row.refresh_from_db()
        assert row.hex_code == "#1E3A5F"

    def test_a_dark_colour_wants_light_text(self, colour):
        """Black swatch, white label: the label has to stay legible on any hex."""
        assert colour.swatch_text_color == "#FFFFFF"

    def test_a_light_colour_wants_dark_text(self, db):
        pale = Color.objects.create(name="Bone", slug="bone", hex_code="#F3EFE7")
        assert pale.swatch_text_color == "#0F172A"

    @pytest.mark.parametrize("value", ["0A1F44", "#0A1F4", "#0A1F444", "#GGGGGG", "blue"])
    def test_a_malformed_hex_is_rejected(self, db, value):
        row = Color(name="Bad", slug="bad", hex_code=value)
        with pytest.raises(ValidationError):
            row.full_clean()

    def test_a_valid_hex_passes_cleaning(self, db):
        row = Color(name="Good", slug="good", hex_code="#0A1F44")
        row.full_clean()

    def test_two_colours_may_share_a_hex(self, db):
        """A swatch is presentation, not identity: "Bone" and "Cream" can look identical."""
        Color.objects.create(name="Bone", slug="bone", hex_code="#F3EFE7")
        Color.objects.create(name="Cream", slug="cream", hex_code="#F3EFE7")
        assert Color.objects.count() == 2


class TestVocabularies:
    def test_material_is_a_row_not_free_text(self, material):
        assert str(material) == "Organic cotton"
        assert material.is_active is True

    def test_fit_is_a_row_not_free_text(self, fit):
        assert str(fit) == "Oversized"

    def test_tag_is_a_row_not_free_text(self, tag):
        assert str(tag) == "New in"

    def test_deactivating_hides_a_row_without_deleting_it(self, material):
        material.is_active = False
        material.save()
        assert Material.objects.filter(pk=material.pk, is_active=False).exists() is True

    def test_slug_is_unique_across_the_vocabulary(self, db):
        from django.db import IntegrityError, transaction

        Material.objects.create(name="Cotton", slug="cotton")
        with pytest.raises(IntegrityError), transaction.atomic():
            Material.objects.create(name="Cotton Again", slug="cotton")

    def test_seo_overrides_are_optional(self, db):
        row = ProductTag(name="Limited", slug="limited")
        row.full_clean()
        assert row.seo_title == ""


class TestSlugsForVocabularies:
    def test_a_blank_slug_is_derived_from_the_name(self, db):
        row = Material.objects.create(name="Ripstop Nylon", slug="")
        assert row.slug == "ripstop-nylon"

    def test_duplicate_names_are_disambiguated(self, db):
        first = Fit.objects.create(name="Relaxed", slug="")
        second = Fit.objects.create(name="Relaxed", slug="")
        assert first.slug == "relaxed"
        assert second.slug == "relaxed-2"
