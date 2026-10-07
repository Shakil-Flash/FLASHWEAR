"""Content and Merchandising Studio Back Office forms (Phase 29).

Provides styled forms for:
- Homepage sections (hero, collections, categories, stories, drops, banners)
- Editorial fashion stories
- Seasonal campaigns
- Manual product merchandising (selection, ordering, scheduling)
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.catalog.image_fetcher import fetch_and_validate_image_file
from apps.catalog.models import Product
from apps.core.models import Campaign, EditorialStory, HomepageSection, SectionProduct
from apps.core.services.content import validate_cta_destination

_INPUT = (
    "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm "
    "focus:border-slate-900 focus:outline-none"
)
_SELECT = (
    "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm "
    "focus:border-slate-900 focus:outline-none"
)
_TEXTAREA = _INPUT
_CHECKBOX = "h-4 w-4 rounded border-slate-300 text-slate-900 focus:ring-slate-900"


class StyledModelForm(forms.ModelForm):
    """Base model form attaching uniform Tailwind CSS styling to widgets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", _CHECKBOX)
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", _SELECT)
            elif isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("class", _TEXTAREA)
            elif hasattr(widget, "input_type"):
                widget.attrs.setdefault("class", _INPUT)


class HomepageSectionForm(StyledModelForm):
    """Form to manage homepage sections and hero presentation."""

    class Meta:
        model = HomepageSection
        fields = [
            "title",
            "subtitle",
            "section_type",
            "is_enabled",
            "display_order",
            "starts_at",
            "ends_at",
            "campaign",
            "desktop_image",
            "mobile_image",
            "desktop_image_url",
            "mobile_image_url",
            "image_alt",
            "overlay_position",
            "primary_cta_text",
            "primary_cta_url",
            "secondary_cta_text",
            "secondary_cta_url",
            "linked_collection",
            "linked_category",
            "linked_drop",
        ]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": _INPUT}),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": _INPUT}),
            "subtitle": forms.Textarea(attrs={"rows": 2, "class": _TEXTAREA}),
        }

    def clean_primary_cta_url(self):
        url = self.cleaned_data.get("primary_cta_url")
        validate_cta_destination(url)
        return url

    def clean_secondary_cta_url(self):
        url = self.cleaned_data.get("secondary_cta_url")
        validate_cta_destination(url)
        return url

    def clean(self):
        cleaned = super().clean()
        starts_at = cleaned.get("starts_at")
        ends_at = cleaned.get("ends_at")
        if starts_at and ends_at and starts_at > ends_at:
            self.add_error("ends_at", _("End date must be after start date."))

        # Process desktop external image if provided and no file uploaded
        desktop_url = (cleaned.get("desktop_image_url") or "").strip()
        if desktop_url and not cleaned.get("desktop_image"):
            try:
                downloaded = fetch_and_validate_image_file(desktop_url)
                cleaned["desktop_image"] = downloaded.file
            except forms.ValidationError as error:
                self.add_error("desktop_image_url", error)

        # Process mobile external image if provided and no file uploaded
        mobile_url = (cleaned.get("mobile_image_url") or "").strip()
        if mobile_url and not cleaned.get("mobile_image"):
            try:
                downloaded = fetch_and_validate_image_file(mobile_url)
                cleaned["mobile_image"] = downloaded.file
            except forms.ValidationError as error:
                self.add_error("mobile_image_url", error)

        return cleaned


class EditorialStoryForm(StyledModelForm):
    """Form to manage lightweight fashion stories."""

    class Meta:
        model = EditorialStory
        fields = [
            "title",
            "slug",
            "subtitle",
            "story",
            "image",
            "image_url",
            "image_alt",
            "primary_cta_text",
            "primary_cta_url",
            "linked_collection",
            "linked_category",
            "linked_products",
            "campaign",
            "display_order",
            "is_published",
            "starts_at",
            "ends_at",
        ]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": _INPUT}),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": _INPUT}),
            "story": forms.Textarea(attrs={"rows": 4, "class": _TEXTAREA}),
            "subtitle": forms.Textarea(attrs={"rows": 2, "class": _TEXTAREA}),
            "linked_products": forms.SelectMultiple(attrs={"class": _SELECT, "size": 6}),
        }

    def clean_primary_cta_url(self):
        url = self.cleaned_data.get("primary_cta_url")
        validate_cta_destination(url)
        return url

    def clean(self):
        cleaned = super().clean()
        starts_at = cleaned.get("starts_at")
        ends_at = cleaned.get("ends_at")
        if starts_at and ends_at and starts_at > ends_at:
            self.add_error("ends_at", _("End date must be after start date."))

        image_url = (cleaned.get("image_url") or "").strip()
        if image_url and not cleaned.get("image"):
            try:
                downloaded = fetch_and_validate_image_file(image_url)
                cleaned["image"] = downloaded.file
            except forms.ValidationError as error:
                self.add_error("image_url", error)

        return cleaned


class CampaignForm(StyledModelForm):
    """Form to manage seasonal campaigns."""

    class Meta:
        model = Campaign
        fields = [
            "title",
            "slug",
            "description",
            "starts_at",
            "ends_at",
            "is_active",
            "banner_image",
            "banner_image_url",
            "banner_alt",
        ]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": _INPUT}),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": _INPUT}),
            "description": forms.Textarea(attrs={"rows": 3, "class": _TEXTAREA}),
        }

    def clean(self):
        cleaned = super().clean()
        starts_at = cleaned.get("starts_at")
        ends_at = cleaned.get("ends_at")
        if starts_at and ends_at and starts_at > ends_at:
            self.add_error("ends_at", _("End date must be after start date."))

        banner_url = (cleaned.get("banner_image_url") or "").strip()
        if banner_url and not cleaned.get("banner_image"):
            try:
                downloaded = fetch_and_validate_image_file(banner_url)
                cleaned["banner_image"] = downloaded.file
            except forms.ValidationError as error:
                self.add_error("banner_image_url", error)

        return cleaned


class SectionProductForm(StyledModelForm):
    """Form to manually feature a product inside a section."""

    product = forms.ModelChoiceField(
        queryset=Product.objects.filter(status=Product.Status.ACTIVE).order_by("name"),
        label=_("Product"),
        widget=forms.Select(attrs={"class": _SELECT}),
    )


    class Meta:
        model = SectionProduct
        fields = [
            "product",
            "display_order",
            "starts_at",
            "ends_at",
            "is_active",
        ]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": _INPUT}),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": _INPUT}),
            "display_order": forms.NumberInput(attrs={"class": _INPUT}),
        }

    def clean(self):
        cleaned = super().clean()
        starts_at = cleaned.get("starts_at")
        ends_at = cleaned.get("ends_at")
        if starts_at and ends_at and starts_at > ends_at:
            self.add_error("ends_at", _("End date must be after start date."))
        return cleaned
