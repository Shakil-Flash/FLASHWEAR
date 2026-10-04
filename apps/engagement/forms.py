"""Storefront forms for reviews (Phase 7).

Only *content* fields live here. The product, the author, the qualifying order and
``verified_purchase`` are server-derived in the service layer and can never arrive through a
form -- that is the whole verified-purchase rule in one sentence.

Widget styling reuses the account area's shared classes so a form on the product page looks
like a form everywhere else.
"""

from __future__ import annotations

from django import forms

from apps.accounts.forms import input_attrs
from apps.engagement.models import Review

RATING_CHOICES = [(n, f"{n} star{'s' if n != 1 else ''}") for n in range(5, 0, -1)]


class ReviewForm(forms.ModelForm):
    """Create/edit a review's content. Bound to the model's content fields only."""

    rating = forms.TypedChoiceField(
        choices=RATING_CHOICES,
        coerce=int,
        widget=forms.Select(attrs=input_attrs()),
        label="Your rating",
    )
    title = forms.CharField(
        max_length=120,
        widget=forms.TextInput(
            attrs=input_attrs(placeholder="Sums it up in a few words", maxlength="120")
        ),
    )
    body = forms.CharField(
        widget=forms.Textarea(
            attrs={
                **input_attrs(placeholder="How did it fit, feel, wear?"),
                "rows": 5,
                "minlength": "3",
            }
        ),
        min_length=3,
        max_length=4000,
    )

    class Meta:
        model = Review
        fields = ("rating", "title", "body")

    def clean_title(self) -> str:
        return (self.cleaned_data["title"] or "").strip()

    def clean_body(self) -> str:
        body = (self.cleaned_data["body"] or "").strip()
        if not body:
            raise forms.ValidationError("A review needs a title and some words.")
        return body
