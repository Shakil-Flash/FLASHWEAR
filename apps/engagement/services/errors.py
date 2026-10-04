"""Domain exceptions for engagement rules.

These are deliberately *not* ``ValidationError``: the same rule failure has to surface as a
Django form error on the server-rendered checkout and as a structured DRF payload on the API,
so each call site maps ``EngagementError`` into its own vocabulary while keeping ``code`` as the
stable, translatable-by-the-client identifier.

Messages are already user-facing English (matching the Phase 7 copy requirements); templates
that re-render them pass them through ``{{ }}``, which escapes.
"""

from __future__ import annotations


class EngagementError(Exception):
    """Base class for promotion/loyalty rule violations raised by the services."""

    code = "engagement_invalid"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code

    @property
    def message(self) -> str:
        return str(self)


class PromotionError(EngagementError):
    """A promotion code failed eligibility or consumption."""

    code = "promotion_invalid"


class LoyaltyError(EngagementError):
    """FLASH Points could not be reserved, consumed or released as asked."""

    code = "loyalty_invalid"


class ReviewError(EngagementError):
    """A review broke a rule the service re-checks defensively (races, direct calls)."""

    code = "review_invalid"
