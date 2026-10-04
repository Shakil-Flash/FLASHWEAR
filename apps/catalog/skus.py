"""SKU construction.

A SKU is a warehouse label: staff scan it, support quotes it over the phone, and it ends up in
order lines. So it has to be **predictable** and **unique**.

Predictable
    :func:`build_sku` derives the value from the row it belongs to -- product, colour, size -- with
    no random component. ``FW-0042-BLK-M`` can be worked out by hand, which is the point of the
    ``FW-<style code>-<colour>-<size>`` convention in fashion retail.

Unique
    The product's id plus its colour and size cannot repeat, because a variant is unique per
    (product, colour, size) -- four database constraints enforce exactly that. So the generated
    value cannot collide either. Should a hand-typed SKU clash anyway, :func:`assign_unique_sku`
    appends the next free numeric suffix instead of raising, and the ``sku`` unique index remains
    the backstop for concurrent writers.

Automation is a convenience, never a rule: ``ProductVariant.sku`` is editable and an admin who
wants ``FW-TS-OVS-BLK-M`` types it and gets it.
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

# Fixed-width tokens keep SKUs sortable and same-length, which matters when they are printed on a
# label sheet. Codes longer than these widths are truncated; collision handling then takes over.
PRODUCT_TOKEN_WIDTH = 4
COLOR_TOKEN_WIDTH = 3
SIZE_TOKEN_WIDTH = 3

_NON_ALPHANUMERIC = re.compile(r"[^A-Z0-9]+")

SKU_PREFIX = "FW"
PLACEHOLDER_TOKENS = {"NA", "NONE", "OS"}

# The colours every fashion label already abbreviates. Truncation alone produces "BLA" for Black
# and "OFF-" for Off White, both of which a warehouse operator has to stop and decode. An unknown
# colour still falls through to truncation, so this is a nicety rather than a dependency.
COLOR_ABBREVIATIONS = {
    "BLACK": "BLK",
    "WHITE": "WHT",
    "OFF WHITE": "OFF",
    "OFF-WHITE": "OFF",
    "GREY": "GRY",
    "GRAY": "GRY",
    "NAVY": "NVY",
    "OLIVE": "OLV",
    "BEIGE": "BEG",
    "BROWN": "BRN",
    "GREEN": "GRN",
    "BLUE": "BLU",
    "RED": "RED",
    "CLAY": "CLY",
    "SLATE": "SLT",
    "SAND": "SND",
    "CHARCOAL": "CHA",
    "CREAM": "CRM",
    "IVORY": "IVR",
    "BURGUNDY": "BRG",
    "MAROON": "MRN",
    "PURPLE": "PPL",
    "PINK": "PNK",
    "YELLOW": "YEL",
    "KHAKI": "KHK",
    "DENIM": "DNM",
}


def _token(value: str | None, width: int) -> str:
    """Turn ``value`` into an upper-case alphanumeric token of at most ``width`` characters."""
    cleaned = _NON_ALPHANUMERIC.sub("", (value or "").upper())
    return cleaned[:width] or "NA"


def color_token(color) -> str:
    """``Black`` -> ``BLK``, ``Sand`` -> ``SND``.

    Three letters is the retail convention. Known colours use :data:`COLOR_ABBREVIATIONS`; anything
    else is truncated. When two distinct colours collapse onto the same token, the suffix logic in
    :func:`assign_unique_sku` keeps the SKUs apart.
    """
    name = (getattr(color, "code", None) or getattr(color, "name", None) or "").strip().upper()
    abbreviated = COLOR_ABBREVIATIONS.get(name)
    if abbreviated:
        return abbreviated[:COLOR_TOKEN_WIDTH]
    return _token(name, COLOR_TOKEN_WIDTH)


def size_token(size) -> str:
    return _token(getattr(size, "code", None) or getattr(size, "name", None), SIZE_TOKEN_WIDTH)


def build_sku(*, product, color=None, size=None) -> str:
    """Return the canonical SKU for a variant.

    ``product`` must be saved: its primary key is part of the value. Called before ``save()`` on a
    brand-new variant, the caller passes the instance it has just created or will create.
    """
    if getattr(product, "pk", None) is None:
        raise ValueError("build_sku needs a saved product: the SKU contains its id.")
    return "-".join(
        (
            SKU_PREFIX,
            _token(str(product.pk).zfill(PRODUCT_TOKEN_WIDTH), PRODUCT_TOKEN_WIDTH),
            color_token(color),
            size_token(size),
        )
    )


def assign_unique_sku(*, product, color=None, size=None, sku: str | None = None) -> str:
    """Return ``sku`` if it is free, or the next free suffixed SKU if it is not.

    Used for *generated* SKUs, where degrading to ``FW-0042-BLK-M-2`` is better than failing a
    background import. Callers handling a hand-typed SKU should not come here -- see
    :func:`create_variant`, which raises instead, because a person needs to be told their value is
    taken.
    """
    from apps.catalog.models import ProductVariant

    candidate = (sku or build_sku(product=product, color=color, size=size)).strip().upper()
    if not ProductVariant.objects.filter(sku=candidate).exists():
        return candidate

    base = _NON_ALPHANUMERIC.sub("-", candidate).strip("-")
    counter = 2
    while ProductVariant.objects.filter(sku=f"{base}-{counter}").exists():
        counter += 1
    return f"{base}-{counter}"


def create_variant(
    *,
    product,
    price,
    color=None,
    size=None,
    sku: str | None = None,
    compare_at_price=None,
    cost_price=None,
    barcode: str = "",
    is_active: bool = True,
    attempts: int = 5,
):
    """Create a variant, resolving its SKU.

    The only reason this exists rather than ``ProductVariant.objects.create()`` is the SKU race:
    two requests generating the same SKU for the same product both pass the "is it free?" check,
    and the second INSERT hits the unique index. Each attempt runs in its own savepoint so a
    failure can be retried, and that retry stays out of every call site.

    An explicit ``sku`` is treated differently on collision: it raises a :class:`ValidationError`
    naming the field, because an operator who typed a value needs to be told it is taken, not
    handed a silently different one. Only *generated* SKUs are suffixed, by
    :func:`assign_unique_sku`.
    """
    from apps.catalog.models import ProductVariant

    if sku is not None:
        explicit = sku.strip().upper()
        if not explicit:
            raise ValidationError(
                {"sku": _("Give the variant a SKU, or leave it blank to generate one.")}
            )
        if ProductVariant.objects.filter(sku=explicit).exists():
            raise ValidationError(
                {"sku": _("That SKU is already used by another variant.")},
                code="duplicate",
            )
        resolved = explicit
    else:
        resolved = None

    for attempt in range(attempts):
        try:
            with transaction.atomic():
                return ProductVariant.objects.create(
                    product=product,
                    sku=resolved or assign_unique_sku(product=product, color=color, size=size),
                    barcode=barcode,
                    color=color,
                    size=size,
                    price=price,
                    compare_at_price=compare_at_price,
                    cost_price=cost_price,
                    is_active=is_active,
                )
        except IntegrityError:
            if resolved or attempt == attempts - 1:
                raise
    raise RuntimeError("unreachable")  # pragma: no cover - the loop returns or raises above
