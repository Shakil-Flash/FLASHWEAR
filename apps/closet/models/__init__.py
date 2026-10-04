"""Closet models.

Two modules: the wardrobe itself (``ClosetItem``) and outfit composition
(``Outfit`` / ``OutfitItem``).
"""

from apps.closet.models.items import ClosetItem
from apps.closet.models.outfits import Outfit, OutfitItem

__all__ = ["ClosetItem", "Outfit", "OutfitItem"]
