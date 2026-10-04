"""Server-side ownership verification (Phase 13).

This is the security core of FLASH Loop. A customer must never be
able to claim ownership simply by submitting ``product_id``,
``order_id`` or ``closet_item_id`` from the browser: the payload
is untrusted, and every path below resolves the row, checks it
against the signed-in user, and only then returns evidence.

What counts as ownership evidence in this phase:

* **A purchased closet row** -- ``ClosetItem`` with
  ``source = PURCHASED`` and a non-null ``variant``. The row was
  itself created by :func:`apps.closet.services.closet.add_purchased_units`
  from a *delivered* order line the user owns, so it is the
  strongest evidence. A **manual** closet row (no variant) is
  clothing the customer already owned before FLASHWEAR: it is
  wardrobe data, not proof of a FLASHWEAR purchase, and is
  deliberately **not** accepted for resale or trade-in.
* **A delivered order line** -- ``OrderItem`` whose order belongs
  to the user and is in ``DELIVERED`` state. Historical orders
  are read-only here; nothing in this module mutates them.

The service returns an :class:`OwnershipEvidence` bundle, never a
boolean, so callers cannot forget *why* an item is eligible. It
never exposes the reasoning to clients -- the API maps failures
to a single generic message.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from apps.catalog.models import Product, ProductVariant
from apps.closet.models import ClosetItem
from apps.loop.services.errors import EligibilityError, OwnershipError
from apps.orders.models import Order, OrderItem

__all__ = ["OwnershipEvidence", "find_ownership_evidence", "verify_ownership"]


@dataclass(frozen=True)
class OwnershipEvidence:
    """What proved the user owns the physical garment."""

    user: object
    product: Product
    variant: ProductVariant | None
    order_item: OrderItem | None
    closet_item: ClosetItem | None

    @property
    def via_closet(self) -> bool:
        """Whether the evidence came through a purchased closet row."""
        return self.closet_item is not None

    @property
    def via_order(self) -> bool:
        """Whether the evidence came through a delivered order line."""
        return self.order_item is not None


def _require_flashwear(product: Product) -> None:
    """Reject third-party goods: only in-house FLASHWEAR merchandise loops."""
    if product.brand is None:
        return
    if product.brand.slug in settings.LOOP_ELIGIBLE_BRAND_SLUGS:
        return
    raise EligibilityError(
        "Only FLASHWEAR merchandise can enter the FLASH Loop in this phase.",
        code="loop_third_party_product",
    )


def _from_closet_item(user, closet_item: ClosetItem) -> OwnershipEvidence:
    """Verify a wardrobe row. Manual rows are not ownership evidence."""
    if closet_item.user_id != user.id:
        raise OwnershipError("That wardrobe item is not yours.", code="loop_not_owner")
    if closet_item.source != ClosetItem.Source.PURCHASED:
        raise OwnershipError(
            "Only purchased FLASHWEAR pieces can enter the FLASH Loop. "
            "Manually added clothing is not a FLASHWEAR purchase.",
            code="loop_manual_closet_item",
        )
    if closet_item.variant_id is None:
        raise OwnershipError(
            "That wardrobe item has no FLASHWEAR product linked to it.",
            code="loop_closet_item_no_variant",
        )
    product = closet_item.variant.product
    _require_flashwear(product)
    return OwnershipEvidence(
        user=user,
        product=product,
        variant=closet_item.variant,
        order_item=closet_item.order_item,
        closet_item=closet_item,
    )


def _from_order_item(user, order_item: OrderItem) -> OwnershipEvidence:
    """Verify a delivered order line belonging to the user."""
    order: Order = order_item.order
    if order.user_id != user.id:
        raise OwnershipError("That order is not yours.", code="loop_not_owner")
    if order.status != Order.Status.DELIVERED:
        raise OwnershipError(
            "Only delivered orders can enter the FLASH Loop.",
            code="loop_order_not_delivered",
        )
    product = order_item.variant.product
    _require_flashwear(product)
    return OwnershipEvidence(
        user=user,
        product=product,
        variant=order_item.variant,
        order_item=order_item,
        closet_item=None,
    )


def _from_variant(user, variant: ProductVariant) -> OwnershipEvidence:
    """Find ownership evidence for a catalogue variant the user owns.

    Preference order: a purchased closet row (strongest, unit-level),
    then any delivered order line containing the variant.
    """
    closet_item = (
        ClosetItem.objects.filter(
            user=user,
            source=ClosetItem.Source.PURCHASED,
            variant=variant,
            status=ClosetItem.Status.ACTIVE,
        )
        .select_related("order_item")
        .first()
    )
    if closet_item is not None:
        return _from_closet_item(user, closet_item)

    order_item = (
        OrderItem.objects.filter(
            order__user=user,
            order__status=Order.Status.DELIVERED,
            variant=variant,
        )
        .select_related("order")
        .first()
    )
    if order_item is not None:
        return _from_order_item(user, order_item)

    raise OwnershipError(
        "We could not verify that you own this FLASHWEAR item. "
        "Only delivered purchases can enter the FLASH Loop.",
        code="loop_no_ownership_evidence",
    )


def find_ownership_evidence(
    user,
    *,
    closet_item_id: int | None = None,
    order_item_id: int | None = None,
    variant_id: int | None = None,
    product_id: int | None = None,
) -> OwnershipEvidence | None:
    """Resolve ownership evidence, or return ``None`` when there is none.

    Called by the eligibility surface; :func:`verify_ownership` is the
    strict form that raises.
    """
    if closet_item_id is not None:
        try:
            closet_item = ClosetItem.objects.select_related("variant__product", "order_item").get(
                pk=closet_item_id
            )
        except ClosetItem.DoesNotExist:
            return None
        try:
            return _from_closet_item(user, closet_item)
        except (OwnershipError, EligibilityError):
            return None

    if order_item_id is not None:
        try:
            order_item = OrderItem.objects.select_related("variant__product", "order").get(
                pk=order_item_id
            )
        except OrderItem.DoesNotExist:
            return None
        try:
            return _from_order_item(user, order_item)
        except (OwnershipError, EligibilityError):
            return None

    if variant_id is not None:
        try:
            variant = ProductVariant.objects.select_related("product").get(pk=variant_id)
        except ProductVariant.DoesNotExist:
            return None
        try:
            return _from_variant(user, variant)
        except (OwnershipError, EligibilityError):
            return None

    if product_id is not None:
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return None
        _require_flashwear(product)
        variants = list(product.variants.all())
        for variant in variants:
            evidence = find_ownership_evidence(user, variant_id=variant.id)
            if evidence is not None:
                return evidence
        return None

    return None


def verify_ownership(
    user,
    *,
    closet_item_id: int | None = None,
    order_item_id: int | None = None,
    variant_id: int | None = None,
    product_id: int | None = None,
) -> OwnershipEvidence:
    """Strict ownership verification. Raises :class:`OwnershipError`.

    The single doorway every loop submission goes through. A payload
    naming another user's order line, closet row or variant is
    rejected here, before any state is written.
    """
    evidence = find_ownership_evidence(
        user,
        closet_item_id=closet_item_id,
        order_item_id=order_item_id,
        variant_id=variant_id,
        product_id=product_id,
    )
    if evidence is None:
        raise OwnershipError(
            "We could not verify that you own this FLASHWEAR item. "
            "Only delivered purchases can enter the FLASH Loop.",
            code="loop_no_ownership_evidence",
        )
    return evidence
