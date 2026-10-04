"""Engagement services.

Three modules, one concern each; callers import the submodule they need
(``from apps.engagement.services import loyalty``). ``errors`` holds the domain exceptions both
the storefront forms and the API translate into their own error vocabulary.
"""

from apps.engagement.services import loyalty, promotions, reviews
from apps.engagement.services.errors import (
    EngagementError,
    LoyaltyError,
    PromotionError,
    ReviewError,
)

__all__ = [
    "EngagementError",
    "LoyaltyError",
    "PromotionError",
    "ReviewError",
    "loyalty",
    "promotions",
    "reviews",
]
