"""FLASH Loop forms (Phase 13).

The create form never accepts product identity from the browser: the
owned-item ``ChoiceField`` is populated server-side from the customer's
own delivered purchases and purchased wardrobe rows, so a tampered
POST cannot point the submission at someone else's garment. Everything
the customer *does* type (condition, notes, title, price) is validated
here, then re-validated by the service layer.
"""

from __future__ import annotations

from decimal import Decimal

from django import forms
from django.conf import settings

from apps.loop.models import LoopItem, LoopItemImage
from apps.loop.services.eligibility import active_loop_item_for
from apps.loop.services.ownership import find_ownership_evidence

__all__ = ["LoopItemCreateForm", "LoopPhotoForm", "owned_item_choices"]


def owned_item_choices(user) -> list[tuple[str, str]]:
    """The customer's own eligible garments, as form choices.

    Two evidence sources, both server-derived:

    * ``closet:<id>`` -- purchased wardrobe rows (source = PURCHASED,
      linked to a FLASHWEAR variant) that are not already inside a
      live loop workflow;
    * ``order:<id>`` -- delivered order lines with units still outside
      the wardrobe, likewise not in a live workflow.

    The value format is opaque to the client: the form only ever
    matches it against rows this query produced, so submitting
    someone else's id fails the choice validation before any
    ownership logic even runs.
    """
    from apps.closet.models import ClosetItem
    from apps.closet.services.closet import remaining_units
    from apps.orders.models import Order

    choices: list[tuple[str, str]] = []

    closet_items = (
        ClosetItem.objects.filter(
            user=user,
            source=ClosetItem.Source.PURCHASED,
            status=ClosetItem.Status.ACTIVE,
        )
        .select_related("variant__product")
        .order_by("-created_at")
    )
    for item in closet_items:
        if item.variant_id is None:
            continue
        evidence = find_ownership_evidence(user, closet_item_id=item.pk)
        if evidence is None:
            continue
        if active_loop_item_for(evidence) is not None:
            continue
        label = item.name
        options = " / ".join(part for part in (item.color, item.size) if part)
        if options:
            label = f"{label} — {options}"
        choices.append((f"closet:{item.pk}", f"{label} (wardrobe)"))

    orders = (
        Order.objects.filter(user=user, status=Order.Status.DELIVERED)
        .prefetch_related("items__variant__product")
        .order_by("-created_at")
    )
    for order in orders:
        for order_item in order.items.all():
            if remaining_units(order_item) <= 0:
                continue
            evidence = find_ownership_evidence(user, order_item_id=order_item.pk)
            if evidence is None:
                continue
            if active_loop_item_for(evidence) is not None:
                continue
            label = order_item.product_name
            if order_item.option_label:
                label = f"{label} — {order_item.option_label}"
            choices.append((f"order:{order_item.pk}", f"{label} (order {order.number})"))

    return choices


class LoopItemCreateForm(forms.Form):
    """Step 1 of the listing flow: pick an owned garment, pick a path."""

    owned_item = forms.ChoiceField(
        label="Your item",
        choices=[],
        help_text="Choose a piece you bought from FLASHWEAR. Ownership is verified on our side.",
    )
    loop_type = forms.ChoiceField(
        label="What would you like to do?",
        choices=LoopItem.Type.choices,
        initial=LoopItem.Type.RESALE,
    )
    condition = forms.ChoiceField(
        label="Condition",
        choices=LoopItem.Condition.choices,
        help_text="Describe it honestly: this is unverified until moderation reviews it.",
    )
    condition_notes = forms.CharField(
        label="Condition notes",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": 2000}),
        help_text="Plain text only, e.g. 'worn twice, minor stitching mark'.",
    )
    title = forms.CharField(
        label="Listing title",
        required=False,
        max_length=200,
        help_text="Resale only. Leave blank to use the product name.",
    )
    description = forms.CharField(
        label="Description",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "maxlength": 5000}),
        help_text="Plain text only. No HTML.",
    )
    asking_price = forms.DecimalField(
        label="Asking price",
        required=False,
        min_value=Decimal("0.01"),
        max_value=settings.LOOP_RESALE_MAX_PRICE,
        max_digits=10,
        decimal_places=2,
        help_text="Resale only. What you'd like to receive.",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["owned_item"].choices = [("", "—")] + owned_item_choices(user)

    def clean_owned_item(self):
        value = self.cleaned_data["owned_item"]
        if not value or ":" not in value:
            raise forms.ValidationError("Choose one of your items.")
        source, _, pk = value.partition(":")
        if source not in ("closet", "order") or not pk.isdigit():
            raise forms.ValidationError("That item could not be recognised.")
        return value

    def clean(self):
        cleaned = super().clean()
        loop_type = cleaned.get("loop_type")
        price = cleaned.get("asking_price")
        if loop_type == LoopItem.Type.RESALE and price is None:
            self.add_error("asking_price", "A resale listing needs an asking price.")
        if loop_type != LoopItem.Type.RESALE and price is not None:
            self.add_error("asking_price", "An asking price only applies to resale.")
        return cleaned

    def evidence_kwargs(self) -> dict:
        """Split the chosen choice into the ownership service's arguments."""
        source, _, pk = self.cleaned_data["owned_item"].partition(":")
        key = "closet_item_id" if source == "closet" else "order_item_id"
        return {key: int(pk)}


class LoopPhotoForm(forms.ModelForm):
    """One photo upload. Ownership is the loop item's, checked by the view."""

    class Meta:
        model = LoopItemImage
        fields = ("image", "kind", "alt_text")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["alt_text"].required = True
        self.fields["alt_text"].help_text = (
            "Describe the photo for screen readers, e.g. 'Front of the hoodie'."
        )
