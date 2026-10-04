"""Resolving what a ticket is *about* (Phase 15).

A ticket may point at an order, a payment, a shipment, a product, a review, a loop item or a
quest. Those pointers are resolved here and nowhere else, which is what makes the rule
checkable in one place:

* a **customer** may reference only rows they own (their order, their review, their loop
  item) or rows that are public (a product, a published quest). Anything else raises
  :class:`ReferenceError` -- and, crucially, the same error whether the row belongs to
  somebody else or does not exist, so the URL space of other people's orders is not
  probeable through the support form;
* a **payment** and a **shipment** are never accepted directly from a customer. They are
  derived from the order they belong to, so a payload cannot attach a stranger's payment by
  guessing its id;
* an **agent** may link any row that exists -- that is the point of an operator tool -- but
  still cannot invent one.

``creator_post_id`` is absent on purpose: ``apps.creator`` is not installed, so ownership of
a creator post cannot be verified and the field is not accepted from any payload.
"""

from __future__ import annotations

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.catalog.models import Product
from apps.engagement.models import Review
from apps.loop.models import LoopItem
from apps.orders.models import Order, Shipment
from apps.payments.models import Payment
from apps.quests.models import Quest
from apps.support.services.errors import ReferenceError

__all__ = ["CUSTOMER_REFERENCE_FIELDS", "link_references", "resolve_for_customer"]

# What a customer may name in a payload, in the order the form presents them.
CUSTOMER_REFERENCE_FIELDS = ("order", "product", "review", "loop_item", "quest")

# Rows an operator may link by primary key.
STAFF_REFERENCE_FIELDS = (
    "order",
    "payment",
    "shipment",
    "product",
    "review",
    "loop_item",
    "quest",
)


def _not_found() -> ReferenceError:
    # One message for "missing" and "not yours": a distinct message would confirm that an
    # id exists, which is exactly what an IDOR probe is looking for.
    return ReferenceError(str(_("We could not find that record to attach to your ticket.")))


def _owned(queryset):
    try:
        return queryset.get()
    except queryset.model.DoesNotExist:
        raise _not_found() from None


def _lookup(value, human_field: str) -> Q:
    """Accept either a primary key or a human handle (``FW-2026-000001``, a slug)."""
    text = str(value).strip()
    if text.isdigit():
        return Q(pk=int(text))
    return Q(**{human_field: text})


def resolve_for_customer(user, **named) -> dict:
    """Return ``{field: instance}`` for every reference the customer named.

    Human handles are accepted because they are what the "Need help?" link carries: an
    order is named by its number and a product by its slug, exactly as the customer sees
    them. Unrecognised keys raise rather than being ignored: a payload that can smuggle an
    unexpected field past validation is how mass assignment starts.
    """
    unknown = set(named) - set(CUSTOMER_REFERENCE_FIELDS)
    if unknown:
        raise ReferenceError(
            _("Unsupported reference: %s.") % ", ".join(sorted(unknown)),
            code="support_bad_reference",
        )

    resolved: dict = {}

    order_value = named.get("order")
    if order_value:
        resolved["order"] = _owned(Order.objects.filter(_lookup(order_value, "number"), user=user))
        # Payment and shipment are derived, never named: a payload cannot attach a
        # stranger's payment by guessing its id.
        payment = Payment.objects.filter(order=resolved["order"]).first()
        if payment is not None:
            resolved["payment"] = payment
        shipment = resolved["order"].shipments.first()
        if shipment is not None:
            resolved["shipment"] = shipment

    product_value = named.get("product")
    if product_value:
        # Products are public, so existence is all there is to prove.
        try:
            resolved["product"] = Product.objects.get(_lookup(product_value, "slug"))
        except Product.DoesNotExist:
            raise _not_found() from None

    review_id = named.get("review")
    if review_id:
        resolved["review"] = _owned(Review.objects.filter(pk=review_id, author=user))

    loop_item_id = named.get("loop_item")
    if loop_item_id:
        resolved["loop_item"] = _owned(LoopItem.objects.filter(pk=loop_item_id, user=user))

    quest_value = named.get("quest")
    if quest_value:
        try:
            resolved["quest"] = Quest.objects.get(_lookup(quest_value, "slug"))
        except Quest.DoesNotExist:
            raise _not_found() from None

    return resolved


def link_references(ticket, **named) -> list[str]:
    """Point an existing ticket at different rows (``None`` clears one).

    Staff-only by construction -- every caller is behind
    :func:`apps.support.permissions.has_capability`. Values are primary keys. Rows are
    validated but **not saved**: the caller writes the ticket and one audit row in the same
    transaction. Returns the names of the fields that changed.
    """
    unknown = set(named) - set(STAFF_REFERENCE_FIELDS)
    if unknown:
        raise ReferenceError(
            _("Unsupported reference: %s.") % ", ".join(sorted(unknown)),
            code="support_bad_reference",
        )

    models = {
        "order": Order,
        "payment": Payment,
        "shipment": Shipment,
        "product": Product,
        "review": Review,
        "loop_item": LoopItem,
        "quest": Quest,
    }
    changed: list[str] = []
    for field, value in named.items():
        if value in (None, "", 0, "0"):
            if getattr(ticket, f"{field}_id") is not None:
                setattr(ticket, f"{field}_id", None)
                changed.append(field)
            continue
        model = models[field]
        try:
            instance = model.objects.get(pk=int(value))
        except (model.DoesNotExist, TypeError, ValueError):
            raise _not_found() from None
        if getattr(ticket, f"{field}_id") != instance.pk:
            setattr(ticket, field, instance)
            changed.append(field)
    return changed
