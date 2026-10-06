"""Product readiness evaluation for catalog management.

Evaluates products against editorial and commerce best practices and produces
clear, non-blocking admin warnings.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _


@dataclass
class ProductReadiness:
    is_ready: bool
    warnings: list[str]

    @property
    def warning_count(self) -> int:
        return len(self.warnings)


def evaluate_product_readiness(product) -> ProductReadiness:
    """Evaluate product readiness for publishing and return non-blocking warnings."""
    warnings: list[str] = []

    # 1. Image checks
    prefetched_images = getattr(product, "_prefetched_objects_cache", {}).get("images")
    if prefetched_images is not None:
        images = list(prefetched_images)
    else:
        images = list(product.images.all())

    if not images:
        warnings.append(str(_("No product images uploaded.")))
    else:
        has_primary = any(img.is_primary for img in images)
        if not has_primary:
            warnings.append(str(_("No primary image designated for product card.")))
        missing_alt = [img for img in images if not (img.alt_text or "").strip()]
        if missing_alt:
            warnings.append(
                str(
                    _(
                        "%(count)d image(s) missing alt text for accessibility."
                    )
                    % {"count": len(missing_alt)}
                )
            )

    # 2. Description checks
    if not (product.short_description or "").strip():
        warnings.append(str(_("Missing short description.")))
    if not (product.description or "").strip():
        warnings.append(str(_("Missing full description.")))

    # 3. Category & Brand checks
    if not getattr(product, "category_id", None):
        warnings.append(str(_("No category assigned.")))
    elif hasattr(product, "category") and product.category and not product.category.is_active:
        warnings.append(
            str(
                _("Assigned category '%(cat)s' is inactive.")
                % {"cat": product.category.name}
            )
        )

    if (
        getattr(product, "brand_id", None)
        and hasattr(product, "brand")
        and product.brand
        and not product.brand.is_active
    ):
        warnings.append(
            str(
                _("Assigned brand '%(brand)s' is inactive.")
                % {"brand": product.brand.name}
            )
        )

    # 4. Variants checks
    prefetched_variants = getattr(product, "_prefetched_objects_cache", {}).get("variants")
    if prefetched_variants is not None:
        variants = list(prefetched_variants)
    else:
        variants = list(product.variants.all())

    if not variants:
        warnings.append(str(_("No SKUs/variants created.")))
    else:
        active_variants = [v for v in variants if v.is_active]
        if not active_variants:
            warnings.append(str(_("No active variants available for purchase.")))
        else:
            missing_price = [v for v in active_variants if v.price is None or v.price <= 0]
            if missing_price:
                warnings.append(
                    str(
                        _(
                            "%(count)d active variant(s) have zero or missing price."
                        )
                        % {"count": len(missing_price)}
                    )
                )

            invalid_compare = [
                v
                for v in active_variants
                if v.compare_at_price is not None and v.compare_at_price <= v.price
            ]
            if invalid_compare:
                warnings.append(
                    str(
                        _(
                            "%(count)d variant(s) have compare-at price "
                            "not exceeding selling price."
                        )
                        % {"count": len(invalid_compare)}
                    )
                )

    # 5. SEO metadata checks
    if not (product.seo_title or "").strip():
        warnings.append(str(_("Missing custom SEO title (using fallback).")))
    if not (product.seo_description or "").strip():
        warnings.append(str(_("Missing custom SEO description (using fallback).")))

    return ProductReadiness(is_ready=len(warnings) == 0, warnings=warnings)
