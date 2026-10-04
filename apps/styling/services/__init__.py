"""Styling services — FLASH DNA and AI Fashion Stylist (Phase 9).

Three modules, one concern each; callers import the submodule they need
(``from apps.styling.services import dna, stylist``). ``errors`` holds the
domain exceptions used by views and forms.

Also exports the top-level service functions for convenience.
"""

from apps.styling.services import dna, stylist
from apps.styling.services.dna import dna_category_list, dna_style_list
from apps.styling.services.errors import (
    DNAError,
    EmptyProfileError,
    MalformedOutputError,
    ProviderError,
    StylistError,
)
from apps.styling.services.stylist import (
    recommend_outfit,
    save_recommended_outfit,
    style_product,
)

__all__ = [
    "DNAError",
    "EmptyProfileError",
    "MalformedOutputError",
    "ProviderError",
    "StylistError",
    "dna",
    "dna_category_list",
    "dna_style_list",
    "recommend_outfit",
    "save_recommended_outfit",
    "style_product",
    "stylist",
]
