"""Upload validation for catalogue imagery.

Shares one rule set with the Phase 2 avatar check and for the same reasons: the declared MIME type
and the file name are both attacker-controlled, so the only trustworthy signal is what the bytes
actually decode as. Everything here is content-first, then name.

Used for every catalogue ``ImageField`` -- product images, category images, brand logos and
collection heroes -- because one permissive path (a brand logo accepting SVG) is enough to serve
script from our own origin.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# Declared extension -> Pillow format. The extension is only ever a secondary check, applied after
# the content has already been verified.
EXTENSIONS_BY_FORMAT = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "WEBP": {".webp"},
}

FORMAT_LABELS = {"JPEG": "JPEG", "PNG": "PNG", "WEBP": "WebP"}


def _extension(filename: str) -> str:
    name = (filename or "").strip().lower()
    return name[name.rfind(".") :] if "." in name else ""


def _allowed_extensions() -> set[str]:
    formats = set(settings.CATALOG_IMAGE_ALLOWED_FORMATS)
    return {ext for fmt in formats for ext in EXTENSIONS_BY_FORMAT.get(fmt, set())}


def _format_label() -> str:
    formats = [
        FORMAT_LABELS.get(fmt, fmt.title()) for fmt in settings.CATALOG_IMAGE_ALLOWED_FORMATS
    ]
    return ", ".join(formats)


def validate_catalog_image(upload):
    """Return ``upload`` if it is a safe, correctly-sized raster image.

    Rejects, in order:

    1. a file over ``CATALOG_IMAGE_MAX_BYTES`` -- the cheap check, and the one that most often
       catches a phone camera's 12-megapixel original;
    2. a declared extension outside the allow-list, before spending CPU on a decode;
    3. a file Pillow cannot structurally verify;
    4. a real format outside the allow-list (so ``photo.jpg`` holding a GIF or an SVG);
    5. an image wider or taller than ``CATALOG_IMAGE_MAX_PIXELS`` -- the ceiling that stops a
       small file from decompressing into gigabytes of memory.

    SVG is absent from ``CATALOG_IMAGE_ALLOWED_FORMATS`` and must stay absent without a sanitiser:
    it is a script container, and it would be served from the storefront's own origin.
    """
    allowed_formats = set(settings.CATALOG_IMAGE_ALLOWED_FORMATS)

    max_bytes = settings.CATALOG_IMAGE_MAX_BYTES
    try:
        size = upload.size
    except (OSError, ValueError) as error:
        # A FieldFile whose backing file is gone (dangling fixture path, deleted
        # storage object) must surface as a validation error -- ``full_clean``
        # only collects ValidationError, so anything else escapes as a 500.
        raise ValidationError(_("That file is not a readable image."), code="unreadable") from error
    if size > max_bytes:
        raise ValidationError(
            _("That image is %(size).1f MB. Keep it under %(limit)d MB."),
            code="too_large",
            params={"size": size / 1024 / 1024, "limit": max_bytes // (1024 * 1024)},
        )

    if _extension(upload.name) not in _allowed_extensions():
        raise ValidationError(
            _("Unsupported file type. Use %(formats)s."),
            code="bad_extension",
            params={"formats": _format_label()},
        )

    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - Pillow is a declared dependency
        raise ValidationError(_("Image uploads are unavailable right now.")) from error

    try:
        upload.seek(0)
        with Image.open(upload) as image:
            image.verify()  # structural check; decodes nothing fully
        upload.seek(0)
        with Image.open(upload) as image:
            detected = (image.format or "").upper()
            width, height = image.size
    except ValidationError:
        raise
    except Exception as error:
        raise ValidationError(_("That file is not a readable image."), code="unreadable") from error

    if detected not in allowed_formats:
        raise ValidationError(
            _("Unsupported image format (%(format)s). Use %(formats)s."),
            code="bad_format",
            params={"format": detected or "unknown", "formats": _format_label()},
        )

    max_pixels = settings.CATALOG_IMAGE_MAX_PIXELS
    if width > max_pixels or height > max_pixels:
        raise ValidationError(
            _("Images must be at most %(limit)d pixels on each side."),
            code="too_large_dimensions",
            params={"limit": max_pixels},
        )

    upload.seek(0)
    return upload
