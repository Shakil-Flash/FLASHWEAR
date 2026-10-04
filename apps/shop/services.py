"""Cart and wishlist business logic.

All write operations that touch a cart or wishlist go through here.
Views, API endpoints and management commands should call these functions
rather than manipulating models directly.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models import ProductVariant
from apps.shop.models import Cart, CartItem

# Maximum quantity per cart item (configurable)
CART_MAX_QUANTITY_PER_ITEM = getattr(settings, "CART_MAX_QUANTITY_PER_ITEM", 99)


class CartError(Exception):
    """Base exception for cart operations."""

    pass


class CartValidationError(CartError):
    """Raised when a cart operation fails validation."""

    pass


class CartMergeError(CartError):
    """Raised when cart merging fails."""

    pass


# =============================================================================
# Cart validation
# =============================================================================


def validate_cart_items(cart) -> list[str]:
    """Validate all items in a cart.

    Returns a list of error messages. Empty list means the cart is valid.
    """
    errors = []
    for item in cart.get_items():
        if not item.variant_is_eligible:
            errors.append(
                _("Variant %(variant)s is no longer available.") % {"variant": item.variant}
            )
        if item.quantity > CART_MAX_QUANTITY_PER_ITEM:
            errors.append(
                _("Quantity for %(variant)s exceeds the maximum allowed (%(max)d).")
                % {"variant": item.variant, "max": CART_MAX_QUANTITY_PER_ITEM}
            )
    return errors


# =============================================================================
# Cart creation and retrieval
# =============================================================================


def get_or_create_cart(request) -> Cart:
    """Get the active cart for the request, creating one if necessary.

    For authenticated users: returns their active cart (creating if needed).
    For anonymous users: uses the session key to find/create a guest cart.
    """
    from apps.shop.models import Cart

    if request.user.is_authenticated:
        cart, _ = Cart.objects.get_or_create(
            user=request.user,
            status=Cart.Status.ACTIVE,
            defaults={"currency": settings.CATALOG_CURRENCY_CODE},
        )
    else:
        # Ensure session exists
        if not request.session.session_key:
            request.session.create()
        cart, _ = Cart.objects.get_or_create(
            session_key=request.session.session_key,
            status=Cart.Status.ACTIVE,
            defaults={"currency": settings.CATALOG_CURRENCY_CODE},
        )
        # Remember the guest cart so the login merge can find it after Django
        # cycles the session key. The session *data* survives the cycle; the key
        # string on the Cart row does not.
        request.session["guest_cart_id"] = cart.pk
    return cart


# =============================================================================
# Cart mutation operations
# =============================================================================


def add_to_cart(
    cart,
    variant: ProductVariant,
    quantity: int = 1,
    *,
    override_quantity: bool = False,
) -> CartItem:
    """Add a variant to the cart, or update the quantity if it already exists.

    Args:
        cart: The cart to add to.
        variant: The ProductVariant to add.
        quantity: Quantity to add (or set if override_quantity=True).
        override_quantity: If True, set quantity to the given value instead of adding.

    Returns:
        The created or updated CartItem.

    Raises:
        CartValidationError: If the variant is not purchasable, or quantity limits exceeded.
    """
    # Validate variant
    if not variant.is_active:
        raise ValidationError(_("This variant is not available."), code="variant_inactive")
    if not variant.product.is_published:
        raise ValidationError(_("This product is not available."), code="product_unavailable")
    if variant.product.category_id and not variant.product.category.is_active:
        raise ValidationError(_("This product's category is inactive."), code="category_inactive")
    if variant.product.brand_id and not variant.product.brand.is_active:
        raise ValidationError(_("This variant's brand is inactive."), code="brand_inactive")

    if quantity < 1:
        raise ValidationError(_("Quantity must be at least 1."), code="quantity_invalid")
    if quantity > CART_MAX_QUANTITY_PER_ITEM:
        raise ValidationError(
            _("Quantity cannot exceed %(max)d.") % {"max": CART_MAX_QUANTITY_PER_ITEM},
            code="quantity_too_high",
        )

    # Check if item already exists in cart
    item, created = CartItem.objects.get_or_create(
        cart=cart,
        variant=variant,
        defaults={
            "quantity": quantity,
            "price_snapshot": variant.price,
        },
    )

    if not created:
        if override_quantity:
            new_quantity = quantity
        else:
            new_quantity = item.quantity + quantity

        if new_quantity > CART_MAX_QUANTITY_PER_ITEM:
            raise ValidationError(
                _("Quantity cannot exceed %(max)d per item.") % {"max": CART_MAX_QUANTITY_PER_ITEM},
                code="quantity_too_high",
            )
        item.quantity = new_quantity
        item.save(update_fields=["quantity", "updated_at"])
    else:
        # New item - price snapshot already set in defaults
        pass

    return item


def update_cart_item_quantity(cart, item_pk: int, quantity: int) -> CartItem:
    """Update the quantity of a cart item.

    Args:
        cart: The cart containing the item.
        item_pk: Primary key of the CartItem.
        quantity: New quantity (must be >= 1).

    Returns:
        The updated CartItem.

    Raises:
        ValidationError: If item not found, or quantity invalid.
    """
    if quantity < 1:
        raise ValidationError(_("Quantity must be at least 1."), code="quantity_invalid")
    if quantity > CART_MAX_QUANTITY_PER_ITEM:
        raise ValidationError(
            _("Quantity cannot exceed %(max)d.") % {"max": CART_MAX_QUANTITY_PER_ITEM},
            code="quantity_too_high",
        )

    try:
        item = cart.items.get(pk=item_pk)
    except CartItem.DoesNotExist as err:
        raise ValidationError(_("Item not found in cart."), code="item_not_found") from err

    item.quantity = quantity
    item.save(update_fields=["quantity", "updated_at"])
    return item


def remove_from_cart(cart, item_pk: int) -> None:
    """Remove an item from the cart.

    Raises:
        ValidationError: If item not found in cart.
    """
    try:
        item = cart.items.get(pk=item_pk)
    except CartItem.DoesNotExist as err:
        raise ValidationError(_("Item not found in cart."), code="item_not_found") from err

    item.delete()


def clear_cart(cart) -> int:
    """Remove all items from the cart.

    Returns:
        Number of items removed.
    """
    count, _ = cart.items.all().delete()
    return count


# =============================================================================
# Cart merging (guest -> authenticated)
# =============================================================================


@transaction.atomic
def merge_carts(guest_cart, user_cart) -> int:
    """Merge a guest cart into an authenticated user's cart.

    For each item in the guest cart:
    - If the user cart already has that variant, add the quantities.
    - Otherwise, move the item to the user cart.

    Returns the number of items merged.

    Raises:
        CartMergeError: If merge would exceed quantity limits.
    """
    merged_count = 0
    for guest_item in guest_cart.items.all():
        # Check if user cart already has this variant
        existing = user_cart.items.filter(variant=guest_item.variant).first()

        if existing:
            new_qty = existing.quantity + guest_item.quantity
            if new_qty > CART_MAX_QUANTITY_PER_ITEM:
                raise CartMergeError(
                    f"Merging {guest_item.variant} would exceed "
                    f"quantity limit ({CART_MAX_QUANTITY_PER_ITEM})."
                )
            existing.quantity = new_qty
            existing.save(update_fields=["quantity", "updated_at"])
        else:
            # Move the item to the user cart
            guest_item.cart = user_cart
            guest_item.save(update_fields=["cart", "updated_at"])

        merged_count += 1

    # Mark guest cart as converted (no longer active)
    guest_cart.status = Cart.Status.CONVERTED
    guest_cart.converted_at = timezone.now()
    guest_cart.save(update_fields=["status", "converted_at", "updated_at"])

    return merged_count


# =============================================================================
# Cart totals and price snapshots
# =============================================================================


def recalculate_price_snapshots(cart) -> int:
    """Refresh all price snapshots in the cart to current variant prices.

    Returns the number of items whose snapshot changed.
    """
    updated = 0
    for item in cart.items.all():
        if item.price_snapshot != item.variant.price:
            item.price_snapshot = item.variant.price
            item.save(update_fields=["price_snapshot", "updated_at"])
            updated += 1
    return updated


def get_cart_totals(cart) -> dict:
    """Calculate cart totals.

    Returns:
        dict with keys: subtotal, item_count, total_quantity, currency
    """
    items = cart.get_items()
    subtotal = sum((item.line_total for item in items), start=Decimal("0.00"))
    item_count = cart.items.count()
    total_quantity = sum(item.quantity for item in items)

    return {
        "subtotal": subtotal,
        "item_count": item_count,
        "total_quantity": total_quantity,
        "currency": cart.currency,
    }


# =============================================================================
# Cart retrieval helpers for views
# =============================================================================


def get_cart_for_request(request) -> Cart | None:
    """Get the current cart for a request (user or session)."""
    from apps.shop.models import Cart

    if request.user.is_authenticated:
        return Cart.objects.filter(user=request.user, status="active").first()
    elif request.session.session_key:
        return Cart.objects.filter(session_key=request.session.session_key, status="active").first()
    return None


def get_price_changes(cart) -> list[dict]:
    """Return list of items whose price has changed since snapshot.

    Each dict contains: item, old_price, new_price, difference.
    """
    changes = []
    for item in cart.items.select_related("variant").all():
        if item.price_snapshot != item.variant.price:
            changes.append(
                {
                    "item": item,
                    "old_price": item.price_snapshot,
                    "new_price": item.variant.price,
                    "difference": item.variant.price - item.price_snapshot,
                }
            )
    return changes
