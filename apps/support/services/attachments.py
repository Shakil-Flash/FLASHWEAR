"""Attachment rules and storage for support tickets (Phase 15).

Evidence is content-checked, never name-checked: a declared type and a file name are both
attacker-controlled, so the only trustworthy signal is what the bytes actually decode as.
The rules are deliberately narrower than the catalogue's image rules because a ticket has no
reason to accept anything executable, embeddable or scripted:

* ``JPEG`` / ``PNG`` / ``WEBP`` -- structural check with Pillow (a decompression bomb is
  rejected by the pixel ceiling, an SVG renamed to ``.png`` fails the format check);
* ``PDF`` -- must begin with the ``%PDF-`` marker;
* ``TXT`` -- must be text: no NUL bytes.

SVG, HTML, ``.exe``, archives and anything else are rejected by the extension allow-list
*before* the content check. Files live under
``settings.SUPPORT_ATTACHMENT_ROOT`` (never ``MEDIA_ROOT``) and have no public URL at all;
the bytes leave through :func:`apps.support.views.attachment_download`, which proves
ownership first.
"""

from __future__ import annotations

import os

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from apps.support.models import SupportAttachment, SupportMessage
from apps.support.services.errors import AttachmentError

__all__ = ["attach", "extension_for", "validate_attachment"]

# Extension -> the content check that must pass for it.
ALLOWED_EXTENSIONS = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".pdf": "pdf",
    ".txt": "text",
}

PDF_MAGIC = b"%PDF-"


def extension_for(filename: str) -> str:
    name = (filename or "").strip().lower()
    return name[name.rfind(".") :] if "." in name else ""


def sanitize_filename(filename: str) -> str:
    """Strip any path a browser (or an attacker) may have included.

    Only the basename survives, control characters are dropped, and an empty result gets a
    neutral name -- the stored name is for display, never for deciding where a file lands
    (that is ``upload_to`` plus the storage's own date path).
    """
    base = os.path.basename(str(filename or "").replace("\\", "/"))
    cleaned = "".join(ch for ch in base if ch.isprintable()).strip().lstrip(".")
    return cleaned[:255] or "attachment"


def validate_attachment(upload) -> str:
    """Validate ``upload`` and return its detected content type.

    Raises :class:`apps.support.services.errors.AttachmentError` with a message that is safe
    to show the uploader. Order matters: the cheap size check first, the extension allow-list
    second (so a hostile file is never decoded), content last.
    """
    max_bytes = getattr(settings, "SUPPORT_ATTACHMENT_MAX_BYTES", 5 * 1024 * 1024)
    size = int(getattr(upload, "size", 0) or 0)
    if size <= 0:
        raise AttachmentError(_("That file is empty."), code="support_attachment_empty")
    if size > max_bytes:
        raise AttachmentError(
            _("That file is %(size).1f MB. Keep it under %(limit)d MB.")
            % {"size": size / 1024 / 1024, "limit": max_bytes // (1024 * 1024)},
            code="support_attachment_too_large",
        )

    ext = extension_for(upload.name)
    kind = ALLOWED_EXTENSIONS.get(ext)
    if kind is None:
        raise AttachmentError(
            _("Unsupported file type. Use JPEG, PNG, WebP, PDF or plain text."),
            code="support_attachment_type",
        )

    try:
        upload.seek(0)
    except Exception:  # an UploadedFile always supports seek; be forgiving if it does not
        pass

    if kind == "pdf":
        head = upload.read(16) or b""
        try:
            upload.seek(0)
        except Exception:  # pragma: no cover - defensive
            pass
        if not head.startswith(PDF_MAGIC):
            raise AttachmentError(
                _("That file is not a readable PDF."), code="support_attachment_unreadable"
            )
        return "application/pdf"

    if kind == "text":
        sample = upload.read(65536) or b""
        try:
            upload.seek(0)
        except Exception:  # pragma: no cover - defensive
            pass
        if b"\x00" in sample:
            raise AttachmentError(
                _("That file is not plain text."), code="support_attachment_unreadable"
            )
        return "text/plain"

    return _validate_image(upload)


def _validate_image(upload) -> str:
    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - Pillow is a declared dependency
        raise AttachmentError(
            _("Image uploads are unavailable right now."), code="support_attachment_unavailable"
        ) from error

    allowed = {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".png": "PNG",
        ".webp": "WEBP",
    }[extension_for(upload.name)]

    try:
        upload.seek(0)
        with Image.open(upload) as image:
            image.verify()
        upload.seek(0)
        with Image.open(upload) as image:
            detected = (image.format or "").upper()
            width, height = image.size
    except Exception as error:
        raise AttachmentError(
            _("That file is not a readable image."), code="support_attachment_unreadable"
        ) from error
    finally:
        try:
            upload.seek(0)
        except Exception:  # pragma: no cover - defensive
            pass

    if detected != allowed:
        raise AttachmentError(
            _("That file is not really a %(expected)s image.") % {"expected": allowed},
            code="support_attachment_type",
        )

    max_pixels = getattr(settings, "SUPPORT_ATTACHMENT_MAX_PIXELS", 8000)
    if width > max_pixels or height > max_pixels:
        raise AttachmentError(
            _("Images must be at most %(limit)d pixels on each side."),
            code="support_attachment_dimensions",
        )
    return f"image/{'jpeg' if detected == 'JPEG' else detected.lower()}"


def attach(message: SupportMessage, *, uploaded_by, upload) -> SupportAttachment:
    """Validate ``upload`` and store it on ``message``.

    The caller has already proved it may write to this ticket; this function only owns the
    bytes. The stored ``file.name`` is the storage's relative path -- never an absolute
    path, never a URL.
    """
    content_type = validate_attachment(upload)
    original = sanitize_filename(upload.name)
    attachment = SupportAttachment(
        message=message,
        original_filename=original,
        content_type=content_type,
        size=int(upload.size),
        uploaded_by=uploaded_by,
    )
    attachment.file.save(original, upload, save=True)
    return attachment
