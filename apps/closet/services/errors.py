"""Domain exceptions for closet and outfit rules (Phase 8).

Same contract as engagement's: services raise these with a user-facing message and a stable
``code``; the forms render ``message`` directly, the API maps ``code`` into a structured
payload. Nothing here is a ``ValidationError`` -- one rule failure has to surface on both
sides without duplicating the rule itself.
"""

from __future__ import annotations


class ClosetError(Exception):
    """A wardrobe or outfit rule was violated (ownership, slots, provenance)."""

    code = "closet_invalid"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code

    @property
    def message(self) -> str:
        return str(self)


class OutfitError(ClosetError):
    """Outfit composition rules: slots, roles, completeness, duplication."""

    code = "outfit_invalid"
