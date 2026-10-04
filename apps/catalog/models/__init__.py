"""Catalogue models.

Importing every concrete model from one place keeps Django's app registry happy and makes the
model list greppable. ``base`` is imported for its side effect of being the shared abstract parent
every concrete model inherits.
"""

from apps.catalog.models.attributes import Color, Fit, Material, ProductTag, Size
from apps.catalog.models.base import SEOMixin, SluggedModel, TimestampedModel
from apps.catalog.models.product import Product, ProductImage, ProductVariant
from apps.catalog.models.taxonomy import Brand, Category, Collection

__all__ = [
    "Brand",
    "Category",
    "Collection",
    "Color",
    "Fit",
    "Material",
    "Product",
    "ProductImage",
    "ProductTag",
    "ProductVariant",
    "SEOMixin",
    "Size",
    "SluggedModel",
    "TimestampedModel",
]
