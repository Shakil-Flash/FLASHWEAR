"""Upload validation for customer-owned account imagery.

Lives apart from ``forms`` on purpose: the same rule has to hang off the model field so
every form that edits a profile -- the storefront form *and* the Django admin's inline --
runs it. A form-only check leaves whichever surface you forgot about accepting whatever
the browser sends.

Content-first, then name: the declared MIME type and the file name are both
attacker-controlled, so the only trustworthy signal is what the bytes actually decode as.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

__all__ = ["AVATAR_EXTENSIONS", "validate_avatar"]

AVATAR_EXTENSIONS = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "WEBP": {".webp"},
}


def _extension(filename: str) -> str:
    name = (filename or "").strip().lower()
    return name[name.rfind(".") :] if "." in name else ""


def validate_avatar(upload) -> object:
    """Return ``upload`` if it is a safe, correctly-sized raster image.

    Rejects, in order: an oversized file, an unsupported format, and a file whose declared
    extension contradicts its actual content. SVG is not in the allow-list and never will be
    without a sanitiser -- an SVG is executable markup served from our own origin.
    """
    allowed_formats = set(settings.ACCOUNT_AVATAR_ALLOWED_FORMATS)
    allowed_extensions = {
        extension for fmt in allowed_formats for extension in AVATAR_EXTENSIONS.get(fmt, set())
    }

    max_bytes = settings.ACCOUNT_AVATAR_MAX_BYTES
    try:
        size = upload.size
    except (OSError, ValueError) as error:
        # A FieldFile whose backing file is gone must surface as a validation
        # error: full_clean only collects ValidationError, and anything else
        # escapes it as a 500 (same rule as validate_catalog_image).
        raise ValidationError(_("That file is not a readable image."), code="unreadable") from error
    if size > max_bytes:
        raise ValidationError(
            _("That image is %(size).1f MB. Keep it under %(limit)d MB."),
            code="too_large",
            params={"size": size / 1024 / 1024, "limit": max_bytes // (1024 * 1024)},
        )

    # Reject on the declared name before spending CPU on a decode, but only after the size
    # check so the customer gets the more useful message first.
    if _extension(upload.name) not in allowed_extensions:
        raise ValidationError(
            _("Unsupported file type. Use PNG, JPEG or WebP."),
            code="bad_extension",
        )

    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - Pillow is a declared dependency
        raise ValidationError(
            _("Image uploads are unavailable right now."),
        ) from error

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
            _("Unsupported image format (%(format)s). Use PNG, JPEG or WebP."),
            code="bad_format",
            params={"format": detected or "unknown"},
        )

    max_pixels = settings.ACCOUNT_AVATAR_MAX_PIXELS
    if width > max_pixels or height > max_pixels:
        raise ValidationError(
            _("Images must be at most %(limit)d pixels on each side."),
            code="too_large_dimensions",
            params={"limit": max_pixels},
        )

    upload.seek(0)
    return upload
