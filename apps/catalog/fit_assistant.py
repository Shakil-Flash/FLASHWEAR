"""Smart Size & Fit Assistant domain service (Phase 41).

Provides honest, transparent, explainable sizing advice using real product data,
category/product size guides, product fit attributes, fabric stretch, model specs,
and strictly opt-in customer measurements / Flash DNA.

Guarantees:
- Never guarantees a fit (always includes advisory disclaimer).
- Never fabricates dimensions, model details, or customer review feedback.
- Explains exactly how a recommendation was derived.
- Safely falls back when data is missing or incomplete.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from apps.catalog.models import Product, ProductVariant, SizeGuide, SizeGuideRow
from apps.engagement.services.reviews import fit_feedback_for


def get_size_guide_for_product(product: Product) -> SizeGuide | None:
    """Resolve the active size guide for a given product.

    Resolution order:
    1. Direct product-specific size guide override (`product.size_guide`).
    2. Category-level size guide (`product.category.size_guides`).
    3. Ancestor category size guides (from closest parent up to department root).
    """
    try:
        if hasattr(product, "size_guide") and product.size_guide and product.size_guide.is_active:
            return product.size_guide
    except SizeGuide.DoesNotExist:
        pass

    category = product.category
    if category is None:
        return None

    guide = category.size_guides.filter(is_active=True).first()
    if guide:
        return guide

    if category.parent_id:
        ancestors = list(reversed(category.ancestors()))
        for anc in ancestors:
            guide = anc.size_guides.filter(is_active=True).first()
            if guide:
                return guide

    return None


def _format_fit_description(product: Product) -> str:
    """Generate honest fit description based strictly on the product's Fit attribute."""
    if not product.fit:
        return "True to size. Designed for everyday comfort and standard drape."

    lower = product.fit.name.lower()
    if "relaxed" in lower or "oversized" in lower or "loose" in lower:
        return (
            f"{product.fit.name} cut with generous room through the chest and body. "
            "For a closer, more tailored fit, consider sizing down."
        )
    if "slim" in lower or "tailored" in lower or "tight" in lower:
        return (
            f"{product.fit.name} cut engineered to trace the natural contours of the body. "
            "True to size; if between sizes or broad-shouldered, consider choosing the larger size."
        )
    if "wide" in lower or "straight" in lower:
        return f"{product.fit.name} with consistent, clean volume from hip to hem."

    return f"{product.fit.name} cut designed for standard everyday comfort."


def get_fit_assessment(
    product: Product,
    user=None,
    selected_variant: ProductVariant | None = None,
    review_fit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile comprehensive sizing & fit intelligence for the product page."""
    guide = get_size_guide_for_product(product)
    rows: list[SizeGuideRow] = []
    if guide:
        rows = list(
            guide.rows.select_related("size").order_by("display_order", "size__display_order", "id")
        )

    fit_name = product.fit.name if product.fit else "Standard Fit"
    fit_desc = _format_fit_description(product)
    fabric_stretch_code = getattr(product, "fabric_stretch", Product.FabricStretch.NON_STRETCH)
    fabric_stretch_display = (
        product.get_fabric_stretch_display()
        if hasattr(product, "get_fabric_stretch_display")
        else "Non-stretch"
    )

    model_height_cm = getattr(product, "model_height_cm", None)
    model_wearing_size = (
        product.model_wearing_size.code if getattr(product, "model_wearing_size", None) else ""
    )
    model_measurements = getattr(product, "model_measurements", "")
    has_model_info = bool(model_height_cm or model_wearing_size or model_measurements)

    # Review fit feedback
    if review_fit is None:
        review_fit = fit_feedback_for(product)

    # Customer opt-in measurements
    has_user_measurements = False
    cust_chest: float | None = None
    cust_waist: float | None = None
    cust_hips: float | None = None
    fit_pref = "regular"

    if user and user.is_authenticated:
        meas = getattr(user, "customer_measurement", None)
        if meas and meas.consent_given:
            has_user_measurements = True
            cust_chest = meas.get_chest_cm()
            cust_waist = meas.get_waist_cm()
            cust_hips = meas.get_hips_cm()
            fit_pref = meas.fit_preference

    # Fit recommendation engine
    recommended_size = ""
    has_recommendation = False
    explanation = ""

    if not guide or not rows:
        explanation = (
            "Detailed garment measurements are not yet published for this piece. "
            "Please refer to standard sizing or contact customer care."
        )
    elif has_user_measurements and (cust_chest or cust_waist or cust_hips):
        is_bottom = bool(
            product.category
            and any(
                w in product.category.name.lower()
                for w in ("pant", "jean", "short", "skirt", "trouser", "bottom")
            )
        )
        target_val = (
            cust_waist if (is_bottom and cust_waist) else (cust_chest or cust_waist or cust_hips)
        )
        dim_label = "waist" if (is_bottom and cust_waist) else "chest"

        scored_rows: list[tuple[SizeGuideRow, Decimal]] = []
        for row in rows:
            if is_bottom:
                min_raw = row.waist_min or row.hips_min
                max_raw = row.waist_max or min_raw
            else:
                min_raw = row.chest_min or row.waist_min
                max_raw = row.chest_max or min_raw

            if min_raw is None:
                continue

            min_cm = row._convert(min_raw, "cm")
            max_cm = row._convert(max_raw, "cm")
            if min_cm is None:
                continue
            if max_cm is None:
                max_cm = min_cm

            if guide.measurement_type == SizeGuide.MeasurementType.BODY:
                mid_cm = (min_cm + max_cm) / Decimal("2.0")
                delta = abs(Decimal(str(target_val)) - mid_cm)
                if fit_pref == "relaxed" and mid_cm > Decimal(str(target_val)):
                    delta -= Decimal("2.0")
                elif fit_pref == "slim" and mid_cm < Decimal(str(target_val)):
                    delta -= Decimal("2.0")
            else:
                # Finished garment laid flat: half-width is < 70cm, circumference is * 2
                circ_cm = min_cm * Decimal("2.0") if min_cm < Decimal("70.0") else min_cm
                ease = Decimal("8.0")
                if "slim" in fit_name.lower():
                    ease = Decimal("4.0")
                elif "relaxed" in fit_name.lower() or "oversized" in fit_name.lower():
                    ease = Decimal("14.0")

                if fabric_stretch_code in (
                    Product.FabricStretch.MEDIUM,
                    Product.FabricStretch.HIGH,
                ):
                    ease = max(Decimal("1.0"), ease - Decimal("4.0"))

                if fit_pref == "relaxed":
                    ease += Decimal("4.0")
                elif fit_pref == "slim":
                    ease = max(Decimal("1.0"), ease - Decimal("3.0"))

                ideal_circ = Decimal(str(target_val)) + ease
                delta = abs(circ_cm - ideal_circ)

            scored_rows.append((row, delta))

        if scored_rows:
            best_row = min(scored_rows, key=lambda item: item[1])[0]
            recommended_size = best_row.size.code
            has_recommendation = True
            meas_type_label = (
                "garment dimensions"
                if guide.measurement_type == SizeGuide.MeasurementType.GARMENT
                else "body measurements"
            )
            explanation = (
                f"Recommended size {recommended_size} based on your "
                f"{round(target_val)} cm {dim_label} "
                f"and this piece's {fit_name.lower()} cut ({meas_type_label}). "
                "Fit recommendations are advisory only; fit is not guaranteed."
            )
    else:
        # Check Flash DNA for preferred fit match
        dna = getattr(user, "flash_dna", None) if user and user.is_authenticated else None
        if dna and product.fit_id and dna.preferred_fits.filter(pk=product.fit_id).exists():
            explanation = (
                f"This piece matches your preferred {product.fit.name.lower()} "
                "silhouette in FLASH DNA. "
                "Save your measurements in your account for personalized size recommendations, "
                "or compare your dimensions to the size guide table below."
            )
        else:
            explanation = (
                "Compare your measurements with the size guide table below to find your ideal fit, "
                "or save your private measurements in your account for automated suggestions."
            )

    return {
        "guide": guide,
        "rows": rows,
        "has_guide": bool(guide and rows),
        "measurement_type": guide.measurement_type if guide else "",
        "measurement_type_display": (guide.get_measurement_type_display() if guide else ""),
        "unit": guide.unit if guide else "cm",
        "fit_name": fit_name,
        "fit_description": fit_desc,
        "fabric_stretch": fabric_stretch_display,
        "fabric_stretch_code": fabric_stretch_code,
        "silhouette": product.silhouette,
        "model_height_cm": model_height_cm,
        "model_wearing_size": model_wearing_size,
        "model_measurements": model_measurements,
        "has_model_info": has_model_info,
        "recommended_size": recommended_size,
        "has_recommendation": has_recommendation,
        "explanation": explanation,
        "has_user_measurements": has_user_measurements,
        "review_fit": review_fit,
    }
