"""Closet services.

Three modules, one concern each; callers import the submodule they need
(``from apps.closet.services import outfits``). ``errors`` holds the domain exceptions the
storefront forms and the API translate into their own error vocabulary.
"""

from apps.closet.services import closet, outfits
from apps.closet.services.errors import ClosetError, OutfitError

__all__ = ["ClosetError", "OutfitError", "closet", "outfits"]
