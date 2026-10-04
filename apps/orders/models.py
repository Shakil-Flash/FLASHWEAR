"""Orders, their line items, address snapshot, audit trail and shipments.

An order is built exactly once from a validated :class:`~apps.shop.CheckoutSession`
(see :mod:`apps.orders.services`) and then owns four frozen copies of what was
agreed: the money (subtotal / shipping / discount / tax / total), the lines
(names, SKUs, unit prices at the time), the delivery address and the shipping
method. Later catalogue edits must never rewrite history, hence the snapshot
columns rather than joins.

Two related decisions worth stating up front:

* ``user`` is **protected**, not cascaded: the account-area convention keeps
  rows for closed accounts, and an order is a financial record.
* :class:`Reservation` lives in :mod:`apps.inventory` and points *here*; orders
  know nothing about stock. That keeps the migration graph a tree
  (shop -> orders <- inventory / payments).
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel

__all__ = ["Order", "OrderAddress", "OrderEvent", "OrderItem", "Shipment", "ShipmentEvent"]


class InvalidTransition(Exception):
    """Raised when a state machine is asked to make an impossible jump."""

    def __init__(self, entity: str, current: str, target: str):
        self.entity = entity
        self.current = current
        self.target = target
        super().__init__(f"{entity} cannot go from {current} to {target}")


def _stamp_for(target: str) -> str | None:
    return {
        Order.Status.PAID: "paid_at",
        Order.Status.CANCELLED: "cancelled_at",
        Order.Status.SHIPPED: "shipped_at",
        Order.Status.DELIVERED: "delivered_at",
    }.get(target)


# =============================================================================
# Order
# =============================================================================


class Order(TimestampedModel):
    r"""One confirmed purchase: money, lines, address snapshot, lifecycle state.

    The status graph (Phase 6)::

        PENDING_PAYMENT -> PAID -> PROCESSING -> SHIPPED -> DELIVERED
                |            \________________________________
                |            PAID/PROCESSING/SHIPPED -> REFUNDED (later phase)
                v
            CANCELLED

    ``REFUNDED`` exists so the enum does not need a breaking change when the
    refund workflow lands, but nothing in Phase 6 can enter it: a refund without
    provider confirmation would be a lie, and there is no provider confirmation
    yet. Cancellation is likewise only reachable while the order is still
    unpaid -- cancelling a paid order implies returning money.
    """

    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", _("Pending payment")
        PAID = "paid", _("Paid")
        PROCESSING = "processing", _("Processing")
        SHIPPED = "shipped", _("Shipped")
        DELIVERED = "delivered", _("Delivered")
        CANCELLED = "cancelled", _("Cancelled")
        REFUNDED = "refunded", _("Refunded")

    ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
        Status.PENDING_PAYMENT: (Status.PAID, Status.CANCELLED),
        Status.PAID: (Status.PROCESSING, Status.REFUNDED),
        Status.PROCESSING: (Status.SHIPPED, Status.REFUNDED),
        Status.SHIPPED: (Status.DELIVERED, Status.REFUNDED),
        Status.DELIVERED: (Status.REFUNDED,),
        Status.CANCELLED: (),
        Status.REFUNDED: (),
    }

    number = models.CharField(
        _("order number"),
        max_length=32,
        unique=True,
        db_index=True,
        help_text=_("Public, non-sequential handle: FW-<date>-<random>."),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="orders",
        verbose_name=_("user"),
    )
    checkout = models.OneToOneField(
        "shop.CheckoutSession",
        on_delete=models.PROTECT,
        related_name="order",
        verbose_name=_("checkout"),
        help_text=_("The validated session this order was built from; also its idempotency key."),
    )
    status = models.CharField(
        _("status"),
        max_length=24,
        choices=Status.choices,
        default=Status.PENDING_PAYMENT,
        db_index=True,
    )
    currency = models.CharField(
        _("currency"),
        max_length=3,
        help_text=_("ISO 4217 code, frozen from the cart at order creation."),
    )

    # Money: all four stored, totals recomputed server-side at creation only.
    subtotal = models.DecimalField(_("subtotal"), max_digits=10, decimal_places=2)
    shipping_amount = models.DecimalField(_("shipping amount"), max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(
        _("discount amount"),
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=_("Promotion plus FLASH Points discount, frozen at order creation (Phase 7)."),
    )
    tax_amount = models.DecimalField(
        _("tax amount"),
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=_("Reserved for tax calculation; always zero in Phase 6."),
    )
    total = models.DecimalField(_("total"), max_digits=10, decimal_places=2)

    # Discount provenance (Phase 7). Plain strings/ints on purpose: engagement owns the
    # Promotion row and orders never imports that app, which keeps the migration graph a tree.
    promotion_code = models.CharField(
        _("promotion code"),
        max_length=32,
        blank=True,
        default="",
        help_text=_("Normalised code the discount came from; for support and reporting."),
    )
    loyalty_points = models.PositiveIntegerField(
        _("FLASH Points redeemed"),
        default=0,
        help_text=_("Points redeemed on this order; already folded into discount_amount."),
    )

    # Shipping method snapshot (rate table, not a FK: rates are settings).
    shipping_code = models.CharField(_("shipping method"), max_length=32)
    shipping_name = models.CharField(_("shipping method name"), max_length=64)
    shipping_estimate = models.CharField(_("delivery estimate"), max_length=64, blank=True)

    # Lifecycle timestamps: exactly when each milestone was reached.
    paid_at = models.DateTimeField(_("paid at"), null=True, blank=True)
    cancelled_at = models.DateTimeField(_("cancelled at"), null=True, blank=True)
    shipped_at = models.DateTimeField(_("shipped at"), null=True, blank=True)
    delivered_at = models.DateTimeField(_("delivered at"), null=True, blank=True)

    class Meta:
        verbose_name = _("order")
        verbose_name_plural = _("orders")
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return self.number

    @property
    def is_cancellable(self) -> bool:
        """Only unpaid orders can be cancelled -- see the class docstring."""
        return self.status == self.Status.PENDING_PAYMENT

    def can_transition_to(self, target: str) -> bool:
        return target in self.ALLOWED_TRANSITIONS.get(self.status, set())

    def transition_to(
        self,
        target: str,
        *,
        actor=None,
        note: str = "",
        event_type: str = "status_changed",
        metadata: dict | None = None,
        save: bool = True,
    ) -> Order:
        """Move to ``target`` and record why. Raises :class:`InvalidTransition`.

        The status string is checked against ``ALLOWED_TRANSITIONS`` rather than
        trusted from the caller, so an admin form or a webhook cannot force an
        illegal jump.
        """
        if target == self.status:
            return self
        if not self.can_transition_to(target):
            raise InvalidTransition("order", self.status, target)

        previous = self.status
        self.status = target
        stamp = _stamp_for(target)
        if stamp and getattr(self, stamp) is None:
            setattr(self, stamp, timezone.now())

        if save:
            update_fields = {"status", "updated_at"}
            if stamp:
                update_fields.add(stamp)
            self.save(update_fields=sorted(update_fields))
        OrderEvent.objects.create(
            order=self,
            event_type=event_type,
            actor=actor,
            note=note
            or _("Status changed from %(from)s to %(to)s.") % {"from": previous, "to": target},
            metadata={"from": previous, "to": target, **(metadata or {})},
        )
        return self


class OrderItem(models.Model):
    """One line of the order, priced and named as it was when the order was placed."""

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="items", verbose_name=_("order")
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="order_items",
        verbose_name=_("variant"),
    )
    sku = models.CharField(_("SKU"), max_length=64)
    product_name = models.CharField(_("product"), max_length=200)
    option_label = models.CharField(
        _("option"), max_length=200, blank=True, help_text=_("e.g. 'Black / M'.")
    )
    quantity = models.PositiveIntegerField(_("quantity"))
    unit_price = models.DecimalField(_("unit price"), max_digits=10, decimal_places=2)
    line_total = models.DecimalField(_("line total"), max_digits=10, decimal_places=2)

    class Meta:
        verbose_name = _("order item")
        verbose_name_plural = _("order items")
        ordering = ("pk",)
        constraints = [
            models.UniqueConstraint(
                fields=["order", "variant"], name="orders_one_line_per_variant"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.product_name} x{self.quantity}"


class OrderAddress(models.Model):
    """Delivery address as it stood when the order was placed.

    ``address`` records provenance and is nullable so deleting an address book
    entry never rewrites (or blocks) an order; the text fields are the truth.
    """

    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name="shipping_address", verbose_name=_("order")
    )
    address = models.ForeignKey(
        "accounts.Address",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
        verbose_name=_("address book entry"),
    )
    full_name = models.CharField(_("full name"), max_length=150)
    phone = models.CharField(_("phone"), max_length=40, blank=True)
    line1 = models.CharField(_("address line 1"), max_length=255)
    line2 = models.CharField(_("address line 2"), max_length=255, blank=True)
    city = models.CharField(_("city"), max_length=120)
    region = models.CharField(_("region / state"), max_length=120, blank=True)
    postal_code = models.CharField(_("postal code"), max_length=20)
    country = models.CharField(_("country"), max_length=2, help_text=_("ISO 3166-1 alpha-2."))

    class Meta:
        verbose_name = _("order address")
        verbose_name_plural = _("order addresses")

    def __str__(self) -> str:
        return f"{self.full_name}, {self.city}, {self.country}"


class OrderEvent(models.Model):
    """Append-only audit trail: what happened to the order, and when.

    Mirrors :class:`apps.accounts.models.AccountEvent` deliberately: same shape,
    same rules (no credentials, no raw addresses in metadata), so a support tool
    can read both the same way.
    """

    class Type(models.TextChoices):
        CREATED = "created", _("Order created")
        PAYMENT_PENDING = "payment_pending", _("Payment initiated")
        PAYMENT_SUCCEEDED = "payment_succeeded", _("Payment succeeded")
        PAYMENT_FAILED = "payment_failed", _("Payment failed")
        PAYMENT_CANCELLED = "payment_cancelled", _("Payment cancelled")
        STATUS_CHANGED = "status_changed", _("Status changed")
        STOCK_RESERVED = "stock_reserved", _("Stock reserved")
        STOCK_CONSUMED = "stock_consumed", _("Stock consumed")
        STOCK_RELEASED = "stock_released", _("Stock released")
        SHIPMENT_CREATED = "shipment_created", _("Shipment created")
        SHIPMENT_UPDATED = "shipment_updated", _("Shipment updated")
        CANCELLED_BY_CUSTOMER = "cancelled_by_customer", _("Cancelled by customer")
        NOTE = "note", _("Note")

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="events", verbose_name=_("order")
    )
    event_type = models.CharField(_("event type"), max_length=32, choices=Type.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("actor"),
        help_text=_("Staff member or customer behind the event; null for system events."),
    )
    note = models.CharField(_("note"), max_length=300, blank=True)
    metadata = models.JSONField(
        _("metadata"), default=dict, blank=True, help_text=_("Small, non-sensitive facts.")
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("order event")
        verbose_name_plural = _("order events")
        ordering = ("-created_at", "-pk")

    def __str__(self) -> str:
        return f"{self.order.number}: {self.event_type}"


# =============================================================================
# Shipments
# =============================================================================


class Shipment(TimestampedModel):
    """A parcel (or the promise of one) attached to a paid order.

    Phase 6 ships the whole order in one shipment; the model is a list rather
    than a one-to-one so split shipments can arrive without a migration.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        PROCESSING = "processing", _("Processing")
        SHIPPED = "shipped", _("Shipped")
        DELIVERED = "delivered", _("Delivered")
        CANCELLED = "cancelled", _("Cancelled")

    ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
        Status.PENDING: (Status.PROCESSING, Status.CANCELLED),
        Status.PROCESSING: (Status.SHIPPED, Status.CANCELLED),
        Status.SHIPPED: (Status.DELIVERED,),
        Status.DELIVERED: (),
        Status.CANCELLED: (),
    }

    # Shipment status -> the order status it implies (only while reachable).
    ORDER_STATUS_FOR = {
        Status.PROCESSING: "processing",
        Status.SHIPPED: "shipped",
        Status.DELIVERED: "delivered",
    }

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="shipments", verbose_name=_("order")
    )
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.PENDING
    )
    method = models.CharField(
        _("method"), max_length=32, help_text=_("Shipping method code frozen from the order.")
    )
    carrier = models.CharField(_("carrier"), max_length=64, blank=True)
    tracking_number = models.CharField(_("tracking number"), max_length=64, blank=True)
    estimated_delivery = models.DateField(_("estimated delivery"), null=True, blank=True)
    shipped_at = models.DateTimeField(_("shipped at"), null=True, blank=True)
    delivered_at = models.DateTimeField(_("delivered at"), null=True, blank=True)

    class Meta:
        verbose_name = _("shipment")
        verbose_name_plural = _("shipments")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.order.number} ({self.status})"

    def can_transition_to(self, target: str) -> bool:
        return target in self.ALLOWED_TRANSITIONS.get(self.status, set())

    def transition_to(
        self, target: str, *, actor=None, note: str = "", metadata: dict | None = None
    ) -> Shipment:
        """Move the shipment and keep the order in step. Raises :class:`InvalidTransition`."""
        if target == self.status:
            return self
        if not self.can_transition_to(target):
            raise InvalidTransition("shipment", self.status, target)

        previous = self.status
        self.status = target
        if target == self.Status.SHIPPED and self.shipped_at is None:
            self.shipped_at = timezone.now()
        if target == self.Status.DELIVERED and self.delivered_at is None:
            self.delivered_at = timezone.now()
        self.save()

        ShipmentEvent.objects.create(
            shipment=self,
            event_type=target,
            actor=actor,
            note=note
            or _("Shipment status changed from %(from)s to %(to)s.")
            % {"from": previous, "to": target},
            metadata={"from": previous, "to": target, **(metadata or {})},
        )

        # Keep the order's headline status aligned when the graph allows it.
        order_target = self.ORDER_STATUS_FOR.get(target)
        if order_target and self.order.status != order_target:
            if self.order.can_transition_to(order_target):
                self.order.transition_to(
                    order_target,
                    actor=actor,
                    note=_("Shipment %(status)s") % {"status": target},
                    metadata={"shipment": self.pk},
                )
        return self


class ShipmentEvent(models.Model):
    """Append-only shipment history (carrier scans would land here too)."""

    shipment = models.ForeignKey(
        Shipment, on_delete=models.CASCADE, related_name="events", verbose_name=_("shipment")
    )
    event_type = models.CharField(_("event type"), max_length=24, choices=Shipment.Status.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("actor"),
    )
    note = models.CharField(_("note"), max_length=300, blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("shipment event")
        verbose_name_plural = _("shipment events")
        ordering = ("-created_at", "-pk")

    def __str__(self) -> str:
        return f"{self.shipment_id}: {self.event_type}"
