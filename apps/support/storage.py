"""Private storage for support attachments.

Ticket evidence (screenshots, receipts, damage photos) is personal data, so it does not live
under ``MEDIA_ROOT`` where the web server serves anything inside it directly. The root comes
from a setting rather than being baked in, which is what lets a deployment point it at a
mounted volume and lets a test point it at a temp directory.

``base_url`` advertises a prefix that nothing serves on purpose: ``.url`` on an attachment
resolves to a path that 404s, so no template, serializer or admin widget can hand out a
working URL by accident. The bytes leave the box only through the authenticated download
view, which checks ownership first.
"""

from __future__ import annotations

from os.path import abspath

from django.conf import settings
from django.core.files.storage import FileSystemStorage

__all__ = ["PrivateAttachmentStorage"]


class PrivateAttachmentStorage(FileSystemStorage):
    """A ``FileSystemStorage`` rooted at ``settings.SUPPORT_ATTACHMENT_ROOT``.

    Both properties are plain (non-cached) descriptors: ``FileSystemStorage`` caches its
    location per instance, which would freeze the value at import time and make
    ``override_settings`` useless in tests.
    """

    setting_name = "SUPPORT_ATTACHMENT_ROOT"

    @property
    def base_location(self) -> str:
        return str(getattr(settings, self.setting_name))

    @property
    def location(self) -> str:
        return abspath(self.base_location)

    @property
    def base_url(self) -> str | None:
        # None makes FileSystemStorage.url raise: an attachment has no public URL at all.
        return None
