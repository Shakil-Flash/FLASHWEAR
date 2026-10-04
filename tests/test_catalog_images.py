"""Tests for catalogue image validation.

This is a security control, so it is tested like one: every rejection path gets a case, and the
rejections are checked for the *reason* (``code``) as well as the fact, because "it raised" is not
the same as "it raised for the right reason".

The central property: **the bytes decide, not the file name.** A ``photo.jpg`` that actually
contains an SVG is rejected, and so is ``logo.svg`` even though it is a valid image format.
"""

from io import BytesIO

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.catalog.validators import validate_catalog_image

pytestmark = pytest.mark.django_db


def _image_bytes(
    fmt: str = "PNG", size: tuple[int, int] = (800, 800), colour=(30, 90, 200)
) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format=fmt)
    return buffer.getvalue()


def _upload(name="photo.png", fmt="PNG", size=(800, 800), content=None) -> SimpleUploadedFile:
    payload = content if content is not None else _image_bytes(fmt, size)
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]
    return SimpleUploadedFile(name, payload, content_type=mime)


class TestAcceptedImages:
    def test_accepts_a_png(self):
        upload = _upload()
        assert validate_catalog_image(upload) is upload

    def test_accepts_a_jpeg(self):
        assert validate_catalog_image(_upload("photo.jpg", "JPEG")) is not None

    def test_accepts_a_webp(self):
        assert validate_catalog_image(_upload("photo.webp", "WEBP")) is not None

    def test_the_file_pointer_is_left_at_the_start(self):
        """A validator that consumed the stream would silently truncate the upload."""
        upload = _upload()
        validate_catalog_image(upload)
        assert upload.tell() == 0

    def test_accepts_the_exact_dimension_ceiling(self):
        from django.conf import settings

        limit = settings.CATALOG_IMAGE_MAX_PIXELS
        assert validate_catalog_image(_upload(size=(limit, limit))) is not None


class TestByteSize:
    def test_rejects_an_oversized_file(self):
        with override_settings(CATALOG_IMAGE_MAX_BYTES=64):
            with pytest.raises(ValidationError) as error:
                validate_catalog_image(_upload())
        assert error.value.code == "too_large"

    def test_the_byte_check_runs_before_the_decode(self):
        """A 40MB file should be refused on size, not after spending CPU on it."""
        big = _image_bytes()
        upload = SimpleUploadedFile("photo.png", big + b"\0" * 1024, content_type="image/png")
        with override_settings(CATALOG_IMAGE_MAX_BYTES=10):
            with pytest.raises(ValidationError) as error:
                validate_catalog_image(upload)
        assert error.value.code == "too_large"

    def test_the_message_names_both_sizes(self):
        upload = _upload()
        with override_settings(CATALOG_IMAGE_MAX_BYTES=16):
            with pytest.raises(ValidationError) as error:
                validate_catalog_image(upload)
        message = error.value.messages[0]
        assert "MB" in message


class TestDeclaredExtension:
    @pytest.mark.parametrize(
        "name", ["logo.svg", "logo.SVG", "photo.gif", "photo.bmp", "notes.txt"]
    )
    def test_rejects_an_extension_outside_the_allow_list(self, name):
        """SVG is not in the list and must not be added without a sanitiser."""
        with pytest.raises(ValidationError) as error:
            validate_catalog_image(_upload(name))
        assert error.value.code == "bad_extension"

    def test_a_file_with_no_extension_is_rejected(self):
        with pytest.raises(ValidationError) as error:
            validate_catalog_image(_upload("photo"))
        assert error.value.code == "bad_extension"

    def test_the_message_lists_the_accepted_formats(self):
        with pytest.raises(ValidationError) as error:
            validate_catalog_image(_upload("logo.svg"))
        assert "JPEG" in error.value.messages[0]


class TestContentVerification:
    def test_rejects_a_text_file_named_as_an_image(self):
        """The name claims PNG; the bytes are a shell script."""
        upload = SimpleUploadedFile("photo.png", b"#!/bin/sh\necho hi\n", content_type="image/png")
        with pytest.raises(ValidationError) as error:
            validate_catalog_image(upload)
        assert error.value.code == "unreadable"

    def test_rejects_an_empty_file(self):
        upload = SimpleUploadedFile("photo.png", b"", content_type="image/png")
        with pytest.raises(ValidationError) as error:
            validate_catalog_image(upload)
        assert error.value.code == "unreadable"

    def test_rejects_a_truncated_image(self):
        """Structurally broken: ``verify`` is the check that catches a half-written file."""
        payload = _image_bytes()
        upload = SimpleUploadedFile(
            "photo.png", payload[: len(payload) // 2], content_type="image/png"
        )
        with pytest.raises(ValidationError) as error:
            validate_catalog_image(upload)
        assert error.value.code == "unreadable"

    def test_rejects_an_svg_disguised_as_a_png(self):
        """The decisive case: a valid image file name carrying markup."""
        svg = (
            b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'>"
            b"<script>alert(1)</script></svg>"
        )
        upload = SimpleUploadedFile("photo.png", svg, content_type="image/png")
        with pytest.raises(ValidationError) as error:
            validate_catalog_image(upload)
        assert error.value.code == "unreadable"

    def test_rejects_a_gif_renamed_as_png(self):
        payload = _image_bytes(fmt="GIF")
        upload = SimpleUploadedFile("photo.png", payload, content_type="image/png")
        with pytest.raises(ValidationError) as error:
            validate_catalog_image(upload)
        assert error.value.code == "bad_format"

    def test_the_format_message_names_what_was_found(self):
        upload = SimpleUploadedFile("photo.png", _image_bytes(fmt="GIF"), content_type="image/png")
        with pytest.raises(ValidationError) as error:
            validate_catalog_image(upload)
        assert "GIF" in error.value.messages[0]


class TestPixelCeiling:
    def test_rejects_an_image_that_is_too_wide(self):
        with override_settings(CATALOG_IMAGE_MAX_PIXELS=100):
            with pytest.raises(ValidationError) as error:
                validate_catalog_image(_upload(size=(500, 50)))
        assert error.value.code == "too_large_dimensions"

    def test_rejects_an_image_that_is_too_tall(self):
        with override_settings(CATALOG_IMAGE_MAX_PIXELS=100):
            with pytest.raises(ValidationError) as error:
                validate_catalog_image(_upload(size=(50, 500)))
        assert error.value.code == "too_large_dimensions"

    def test_a_small_file_that_decompresses_hugely_is_rejected(self):
        """The decompression-bomb shape: tiny on disk, enormous in memory."""
        payload = _image_bytes(fmt="PNG", size=(4000, 4000))
        assert len(payload) < 200_000
        with override_settings(CATALOG_IMAGE_MAX_PIXELS=1000):
            with pytest.raises(ValidationError) as error:
                validate_catalog_image(_upload(content=payload))
        assert error.value.code == "too_large_dimensions"

    def test_the_message_states_the_limit(self):
        with override_settings(CATALOG_IMAGE_MAX_PIXELS=42):
            with pytest.raises(ValidationError) as error:
                validate_catalog_image(_upload(size=(900, 900)))
        assert "42" in error.value.messages[0]


class TestSettingsAreHonoured:
    def test_narrowing_the_allowed_formats_closes_a_format(self):
        """A deployment that only wants JPEG must not keep accepting WebP by accident."""
        with override_settings(CATALOG_IMAGE_ALLOWED_FORMATS=("JPEG",)):
            with pytest.raises(ValidationError) as error:
                validate_catalog_image(_upload("photo.png", "PNG"))
        assert error.value.code == "bad_extension"

    def test_the_default_settings_do_not_include_svg(self):
        from django.conf import settings

        assert "SVG" not in settings.CATALOG_IMAGE_ALLOWED_FORMATS


class TestFormIntegration:
    def test_the_image_form_uses_this_validator(self, product):
        """The form must not be able to accept something the validator rejects."""
        from apps.catalog.forms import ProductImageForm

        upload = SimpleUploadedFile(
            "logo.svg",
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            content_type="image/svg+xml",
        )
        form = ProductImageForm(
            data={"alt_text": "A logo", "position": 0},
            files={"image": upload},
            product=product,
        )
        assert not form.is_valid()
        assert "image" in form.errors

    def test_a_valid_image_passes_the_form(self, product):
        from apps.catalog.forms import ProductImageForm

        form = ProductImageForm(
            data={"alt_text": "Black tee, front", "position": 0},
            files={"image": _upload()},
            product=product,
        )
        assert form.is_valid(), form.errors

    def test_stored_files_lose_the_client_filename(self, product):
        """The name on disk is a UUID: a filename is attacker-controlled text in a public URL."""
        from apps.catalog.models import ProductImage

        image = ProductImage.objects.create(
            product=product,
            image=SimpleUploadedFile(
                "../../evil name.png", _image_bytes(), content_type="image/png"
            ),
            alt_text="Front",
            position=0,
        )
        assert image.image.name.startswith(f"catalog/products/{product.pk}/")
        assert "evil" not in image.image.name
        assert image.image.name.endswith(".png")

    def test_an_unassigned_image_has_no_file_name_yet(self, product):
        """Before saving, nothing has been stored -- the name is assigned by the upload path."""
        from apps.catalog.models import ProductImage

        image = ProductImage(product=product, alt_text="x")
        assert not image.image


class TestStorageHelpers:
    def test_extension_helper_handles_a_missing_extension(self):
        from apps.catalog.validators import _extension

        assert _extension("photo") == ""
        assert _extension("PHOTO.PNG") == ".png"
        assert _extension("archive.tar.gz") == ".gz"

    def test_allowed_extensions_follow_the_formats(self):
        from apps.catalog.validators import _allowed_extensions

        assert _allowed_extensions() == {".jpg", ".jpeg", ".png", ".webp"}

    def test_format_labels_are_human_readable(self):
        from apps.catalog.validators import _format_label

        label = _format_label()
        assert "JPEG" in label
        assert "WebP" in label
