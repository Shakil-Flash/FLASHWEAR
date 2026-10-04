"""Closet and outfit forms (Phase 8).

Only *content* fields live here. Ownership (``user``), provenance (``source``,
``variant``, ``order_item``, ``unit``), lifecycle (``status``) and outfit slots (``role``,
``position``) are server-derived -- no form field, payload or hidden input can carry them,
which is the same rule reviews and purchases follow.

Every widget reuses the account area's shared input classes, so a closet form looks like a
form everywhere else.
"""

from __future__ import annotations

from django import forms

from apps.accounts.forms import input_attrs
from apps.closet.models import ClosetItem, Outfit
from apps.closet.services.closet import SORT_CHOICES

__all__ = [
    "ClosetFilterForm",
    "ClosetItemForm",
    "OutfitAddItemForm",
    "OutfitForm",
    "PurchaseAddForm",
]

_BLANK = ("", "All")


def _select(**extra):
    return forms.Select(attrs=input_attrs(**extra))


class ClosetItemForm(forms.ModelForm):
    """Add or edit a manual wardrobe piece's content (including its optional photo)."""

    class Meta:
        model = ClosetItem
        fields = (
            "category",
            "name",
            "brand",
            "color",
            "size",
            "material",
            "season",
            "occasion",
            "style",
            "notes",
            "image",
        )
        widgets = {
            "category": _select(),
            "name": forms.TextInput(
                attrs=input_attrs(placeholder="e.g. Black jeans", maxlength="180")
            ),
            "brand": forms.TextInput(attrs=input_attrs(placeholder="e.g. Levi's", maxlength="120")),
            "color": forms.TextInput(attrs=input_attrs(placeholder="e.g. Black", maxlength="60")),
            "size": forms.TextInput(attrs=input_attrs(placeholder="e.g. W32", maxlength="32")),
            "material": forms.TextInput(
                attrs=input_attrs(placeholder="e.g. Denim", maxlength="120")
            ),
            "season": _select(),
            "occasion": _select(),
            "style": _select(),
            "notes": forms.Textarea(attrs={**input_attrs(), "rows": 3}),
            "image": forms.ClearableFileInput(
                attrs=input_attrs(accept="image/png,image/jpeg,image/webp")
            ),
        }

    def clean_name(self) -> str:
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Give the piece a name.")
        return name


class ClosetFilterForm(forms.Form):
    """Server-side filtering for the closet page.

    Brand and colour offer only the values *this customer's* wardrobe already contains
    (passed in by the view), so a filter cannot be used to probe anyone else's data or to
    conjure values that match nothing.
    """

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(
            attrs=input_attrs(placeholder="Search name, brand or notes", maxlength="120")
        ),
    )
    category = forms.ChoiceField(required=False, choices=[_BLANK], label="Category")
    source = forms.ChoiceField(
        required=False,
        choices=[_BLANK, *ClosetItem.Source.choices],
        label="Source",
    )
    season = forms.ChoiceField(
        required=False, choices=[_BLANK, *ClosetItem.Season.choices], label="Season"
    )
    occasion = forms.ChoiceField(
        required=False, choices=[_BLANK, *ClosetItem.Occasion.choices], label="Occasion"
    )
    style = forms.ChoiceField(
        required=False, choices=[_BLANK, *ClosetItem.Style.choices], label="Style"
    )
    color = forms.ChoiceField(required=False, choices=[_BLANK], label="Colour")
    brand = forms.ChoiceField(required=False, choices=[_BLANK], label="Brand")
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("active", "Active"),
            ("archived", "Archived"),
            ("all", "Active + archived"),
        ],
        initial="active",
        label="Show",
    )
    sort = forms.ChoiceField(
        required=False,
        choices=[(key, key.replace("_", " ")) for key in SORT_CHOICES],
        initial="newest",
        label="Sort",
    )

    def __init__(self, *args, category_choices=(), color_choices=(), brand_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].choices = [(_BLANK[0], "All"), *category_choices]
        self.fields["color"].choices = [(_BLANK[0], "All"), *color_choices]
        self.fields["brand"].choices = [(_BLANK[0], "All"), *brand_choices]


class PurchaseAddForm(forms.Form):
    """Confirm moving a delivered order line's units into the wardrobe.

    The order line itself arrives as a separate id the view resolves through the
    customer's own orders (someone else's line is a 404); this form only carries the
    choices the customer actually decides.
    """

    category = forms.ChoiceField(
        choices=ClosetItem.Category.choices,
        widget=_select(),
        label="Wardrobe category",
        help_text="Confirm where this piece lives in your closet.",
    )
    units = forms.IntegerField(
        min_value=1,
        max_value=99,
        initial=1,
        widget=forms.NumberInput(attrs=input_attrs(min="1", max="99")),
        label="How many",
    )


class OutfitForm(forms.ModelForm):
    """Outfit metadata (name, description, tagging). Status moves through services only."""

    class Meta:
        model = Outfit
        fields = ("name", "description", "occasion", "season", "style")
        widgets = {
            "name": forms.TextInput(
                attrs=input_attrs(placeholder="e.g. Friday office", maxlength="120")
            ),
            "description": forms.Textarea(attrs={**input_attrs(), "rows": 2}),
            "occasion": _select(),
            "season": _select(),
            "style": _select(),
        }

    def clean_name(self) -> str:
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Give the outfit a name.")
        return name


class OutfitAddItemForm(forms.Form):
    """Pick a wardrobe piece (id resolved through the owner's active items in the view)."""

    closet_item = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    note = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(
            attrs=input_attrs(placeholder="Optional note, e.g. rolled cuffs", maxlength="200")
        ),
    )

    def clean_note(self) -> str:
        return (self.cleaned_data.get("note") or "").strip()
