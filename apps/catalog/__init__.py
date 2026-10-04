"""Catalog app: products, variants, fashion taxonomy and storefront catalogue pages.

Phase 3 lives in a single app on purpose. Product, ProductVariant, Category, Brand, Collection
and the fashion attributes are one bounded domain: they reference each other on nearly every
query and every template. Splitting them across ``apps.products`` / ``apps.categories`` /
``apps.collections`` would buy three migration packages and three import graphs in exchange for
nothing -- the seam that eventually justifies a split is inventory (which owns stock and will
need its own app), not the taxonomy. See README "Catalog architecture" for the full rationale.
"""
