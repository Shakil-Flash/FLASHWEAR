"""Styling domain exceptions (Phase 9).

Each exception maps to a user-facing message and an error code that the
views can catch and render consistently.
"""

from __future__ import annotations


class StylistError(Exception):
    """Base class for stylist service errors."""

    def __init__(self, message: str, code: str = "stylist_error"):
        self.message = message
        self.code = code
        super().__init__(self.message)


class DNAError(StylistError):
    """FLASH DNA profile operations."""


class EmptyProfileError(StylistError):
    """The user's FLASH DNA has no preferences set."""


class ProviderError(StylistError):
    """AI provider is unavailable or misconfigured."""


class AIDisabledError(StylistError):
    """AI stylist is globally disabled via settings."""


class MalformedOutputError(StylistError):
    """The AI returned content that does not match the expected schema."""


class NotConfiguredError(StylistError):
    """The requested feature is not configured."""


class ProductUnavailableError(StylistError):
    """The product referenced in a stylist request cannot be found."""


class NotSignedInError(StylistError):
    """The user must be signed in to receive styling advice."""
