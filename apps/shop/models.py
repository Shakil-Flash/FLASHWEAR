"""Shopping cart and cart item models.

A Cart is the customer's working bag. It may belong to an authenticated user or a
guest session. It has an explicit lifecycle state so we can reason about it
programmatically rather than relying on scattered boolean flags.

A CartItem is one purchasable configuration (ProductVariant) at a specific
quantity. The catalogue guarantees (product, color, size) uniqueness, so a
CartItem can point to exactly one ProductVariant and we get the full SKU
semantics for free.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel


class CartManager(models.Manager):
    """Custom manager for Cart with helper methods."""

    def get_active_for_user(self, user):
        """Return the user's active cart, or None."""
        return self.filter(user=user, status=Cart.Status.ACTIVE).first()

    def get_or_create_active_for_user(self, user):
        """Get or create the user's active cart."""
        cart, _ = self.get_or_create(user=user, status=Cart.Status.ACTIVE)
        return cart

    def get_active_for_session(self, session_key: str):
        """Return the active cart for a guest session."""
        return self.filter(session_key=session_key, status=Cart.Status.ACTIVE).first()


class Cart(TimestampedModel):
    """A customer's working bag.

    States:
    - ACTIVE: the normal shopping state; items can be added, removed, modified.
    - CONVERTED: the cart has been handed off to checkout (Phase 6).
    - ABANDONED: explicitly abandoned or expired.

    Only one ACTIVE cart per authenticated user is enforced by a unique
    constraint. Guest carts are identified by session key.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        CONVERTED = "converted", _("Converted")
        ABANDONED = "abandoned", _("Abandoned")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="carts",
        null=True,
        blank=True,
        verbose_name=_("user"),
        help_text=_("Authenticated customer. Null for guest carts."),
    )
    session_key = models.CharField(
        _("session key"),
        max_length=40,
        blank=True,
        db_index=True,
        help_text=_("Django session key for guest carts. Empty for authenticated users."),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        help_text=_("Lifecycle state of this cart."),
    )
    currency = models.CharField(
        _("currency"),
        max_length=3,
        default="USD",
        help_text=_("ISO 4217 currency code. Matches the storefront display currency at creation."),
    )
    converted_at = models.DateTimeField(
        _("converted at"),
        null=True,
        blank=True,
        help_text=_("When this cart was converted to an order (Phase 6)."),
    )
    abandoned_at = models.DateTimeField(
        _("abandoned at"),
        null=True,
        blank=True,
        help_text=_("When this cart was marked abandoned."),
    )

    objects = CartManager()

    class Meta:
        verbose_name = _("cart")
        verbose_name_plural = _("carts")
        ordering = ("-created_at",)
        constraints = [
            # One active cart per authenticated user
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(status="active"),
                name="shop_cart_one_active_per_user",
            ),
            # One active cart per guest session. Scoped to guest carts: authenticated
            # carts all carry an empty session_key, and a unique index on "" would
            # allow only one signed-in customer in the whole database.
            models.UniqueConstraint(
                fields=["session_key"],
                condition=Q(status="active") & ~Q(session_key=""),
                name="shop_cart_one_active_per_session",
            ),
            # Either user or session_key, but not both
            models.CheckConstraint(
                condition=(Q(user__isnull=False) & Q(session_key=""))
                | (Q(user__isnull=True) & ~Q(session_key="")),
                name="cart_user_xor_session",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="shop_cart_user_status_idx"),
            models.Index(fields=["session_key", "status"], name="shop_cart_session_status_idx"),
        ]

    def __str__(self) -> str:
        if self.user_id:
            return f"Cart<{self.user.email}>"
        return f"Cart<guest:{self.session_key[:8]}...>"

    @property
    def is_guest(self) -> bool:
        return self.user_id is None

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    def get_items(self):
        """Return all items with their variants eager-loaded."""
        return self.items.select_related("variant__product", "variant__color", "variant__size")

    def get_item_count(self) -> int:
        """Total quantity of all items in the cart."""
        return self.items.aggregate(total=Sum("quantity"))["total"] or 0

    def get_subtotal(self) -> Decimal:
        """Sum of (quantity * current variant price) for all items."""
        from decimal import Decimal

        total = Decimal("0.00")
        for item in self.get_items():
            total += item.line_total
        return total

    def is_empty(self) -> bool:
        return not self.items.exists()

    def validate_items(self) -> list[str]:
        """Validate all items in the cart.

        Returns a list of error messages. Empty list means valid.
        """
        from apps.shop.services import validate_cart_items

        return validate_cart_items(self)


class CartItem(TimestampedModel):
    """One line in a cart: a specific variant at a specific quantity."""

    cart = models.ForeignKey(
        "Cart",
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("cart"),
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="cart_items",
        verbose_name=_("variant"),
    )
    quantity = models.PositiveIntegerField(
        _("quantity"),
        default=1,
        validators=[MinValueValidator(1)],
        help_text=_("Quantity of this variant in the cart."),
    )
    # Price snapshot: the variant's price at the time it was added/updated.
    # Used to detect price changes and show the customer the difference.
    price_snapshot = models.DecimalField(
        _("price snapshot"),
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text=_("Variant price at time of add/update. Used to detect price changes."),
    )

    class Meta:
        verbose_name = _("cart item")
        verbose_name_plural = _("cart items")
        ordering = ("created_at",)
        constraints = [
            # One row per (cart, variant)
            models.UniqueConstraint(
                fields=["cart", "variant"],
                name="shop_cart_item_unique_per_cart_variant",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gte=1),
                name="shop_cart_item_quantity_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["cart", "variant"], name="shop_cart_item_cart_var_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.variant} x {self.quantity}"

    @property
    def line_total(self):
        """Current authoritative price * quantity.

        Totals shown to the customer are always recomputed from the live variant
        price; ``price_snapshot`` exists to *detect* changes, not to freeze what the
        customer is charged.
        """
        from decimal import Decimal

        return (self.variant.price * self.quantity).quantize(Decimal("0.01"))

    @property
    def line_total_with_snapshot(self):
        """Line total as it was when the item was added (for 'price when added' display)."""
        from decimal import Decimal

        if self.price_snapshot is not None:
            return (self.price_snapshot * self.quantity).quantize(Decimal("0.01"))
        return self.line_total

    @property
    def price_changed(self) -> bool:
        """Whether the live variant price differs from the snapshot."""
        if self.price_snapshot is None:
            return False
        return self.price_snapshot != self.variant.price

    @property
    def variant_is_eligible(self) -> bool:
        """Whether this variant can still be purchased."""
        variant = self.variant
        return (
            variant.is_active
            and variant.product.is_published
            and variant.product.category.is_active
            and (variant.product.brand_id is None or variant.product.brand.is_active)
        )

    def update_price_snapshot(self) -> None:
        """Refresh the price snapshot to the variant's current price."""
        self.price_snapshot = self.variant.price
        self.save(update_fields=["price_snapshot", "updated_at"])


# Checkout models
# =============================================================================


class CheckoutSession(TimestampedModel):
    """A checkout in progress: chosen address, shipping method and validated totals.

    Phase 5 stops at ``VALIDATED`` -- that is the handoff boundary. Phase 6 turns a
    validated session into an order and a payment; no payment instrument or order
    number ever lives on this model.

    A session is deliberately *not* a lock on the cart. The customer may go back and
    edit the bag after validating; the view detects that the snapshot no longer matches
    the cart and reopens the session rather than silently charging a stale total.
    """

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        VALIDATED = "validated", _("Validated")
        CONVERTED = "converted", _("Converted to order")
        ABANDONED = "abandoned", _("Abandoned")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="checkout_sessions",
        verbose_name=_("user"),
    )
    cart = models.ForeignKey(
        "Cart",
        on_delete=models.CASCADE,
        related_name="checkout_sessions",
        verbose_name=_("cart"),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
        help_text=_("Lifecycle state of this checkout."),
    )
    shipping_address = models.ForeignKey(
        "accounts.Address",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("shipping address"),
        help_text=_("Delivery address chosen from the customer's address book."),
    )
    shipping_method_code = models.CharField(
        _("shipping method"),
        max_length=32,
        blank=True,
        help_text=_("Code of the selected shipping method, e.g. 'standard'."),
    )
    subtotal = models.DecimalField(
        _("subtotal"),
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=_("Cart subtotal at validation time."),
    )
    shipping_amount = models.DecimalField(
        _("shipping amount"),
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=_("Shipping cost at validation time."),
    )
    total = models.DecimalField(
        _("total"),
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=_("Subtotal plus shipping at validation time."),
    )
    validated_at = models.DateTimeField(
        _("validated at"),
        null=True,
        blank=True,
        help_text=_("When the checkout last passed validation."),
    )
    snapshot = models.JSONField(
        _("validated snapshot"),
        default=dict,
        blank=True,
        help_text=_(
            "Line items, totals and address as they were when validated. "
            "Phase 6 builds the order from this."
        ),
    )

    class Meta:
        verbose_name = _("checkout session")
        verbose_name_plural = _("checkout sessions")
        ordering = ("-created_at",)
        constraints = [
            # One open checkout per customer.
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(status="open"),
                name="shop_checkout_one_open_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="shop_checkout_user_status_idx"),
        ]

    def __str__(self) -> str:
        return f"Checkout<{self.user.email}:{self.status}>"

    @property
    def is_validated(self) -> bool:
        return self.status == self.Status.VALIDATED and bool(self.validated_at)


# Wishlist models
# =============================================================================


class Wishlist(TimestampedModel):
    """A customer's saved items.

    One wishlist per authenticated user. Created on demand.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wishlist",
        verbose_name=_("user"),
    )

    class Meta:
        verbose_name = _("wishlist")
        verbose_name_plural = _("wishlists")

    def __str__(self) -> str:
        return f"Wishlist<{self.user.email}>"

    def get_item_count(self) -> int:
        return self.items.count()

    def has_product(self, product) -> bool:
        return self.items.filter(product=product).exists()

    def has_variant(self, variant) -> bool:
        return self.items.filter(variant=variant).exists()


class WishlistItem(TimestampedModel):
    """A saved item in a wishlist.

    Can be either:
    - Product-level: just the garment (no colour/size commitment yet).
    - Variant-level: specific colour/size the customer intends to buy.

    A wishlist cannot contain the same product twice, nor the same variant twice.
    """

    wishlist = models.ForeignKey(
        "Wishlist",
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("wishlist"),
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="wishlist_items",
        verbose_name=_("product"),
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.CASCADE,
        related_name="wishlist_items",
        null=True,
        blank=True,
        verbose_name=_("variant"),
        help_text=_("Optional: specific colour/size. Leave empty for general interest."),
    )
    note = models.CharField(
        _("note"),
        max_length=200,
        blank=True,
        help_text=_("Personal reminder, e.g. 'Birthday gift for Mom'."),
    )

    class Meta:
        verbose_name = _("wishlist item")
        verbose_name_plural = _("wishlist items")
        ordering = ("-created_at",)
        constraints = [
            # One wishlist item per product per wishlist (when variant is null)
            models.UniqueConstraint(
                fields=["wishlist", "product"],
                condition=Q(variant__isnull=True),
                name="wishlist_unique_product_no_variant",
            ),
            # One wishlist item per variant per wishlist (when variant is set)
            models.UniqueConstraint(
                fields=["wishlist", "variant"],
                condition=~Q(variant__isnull=True),
                name="wishlist_unique_variant",
            ),
        ]

    def __str__(self) -> str:
        if self.variant:
            return f"{self.product.name} ({self.variant})"
        return self.product.name

    @property
    def display_name(self) -> str:
        if self.variant:
            return f"{self.product.name} ({self.variant})"
        return self.product.name

    @property
    def is_variant_specific(self) -> bool:
        return self.variant is not None

    def get_absolute_url(self) -> str:
        if self.variant:
            return self.variant.get_absolute_url()
        return self.product.get_absolute_url()

    def clean(self):
        super().clean()
        if self.variant and self.variant.product_id != self.product_id:
            from django.core.exceptions import ValidationError
            from django.utils.translation import gettext_lazy as _

            raise ValidationError(
                {"variant": _("That variant belongs to a different product.")},
                code="variant_product_mismatch",
            )
