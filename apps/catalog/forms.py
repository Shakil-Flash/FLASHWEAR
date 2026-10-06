"""Catalogue forms.

Phase 3 has no customer-facing forms -- buying, reviewing and account management of products come
later -- so these exist for two reasons:

1. **the admin gets good errors.** ``ModelForm`` turns a database constraint into a field-level
   message ("This combination already exists") instead of a traceback, which matters most for the
   duplicate-variant rule;
2. **the rules are testable without HTTP.** ``validate_variant_matrix`` is the single place that
   decides a colour/size pair may be used, and both the admin form and the tests call it.

Image fields run :func:`apps.catalog.validators.validate_catalog_image`, the same content-first
check the Phase 2 avatar uses.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.catalog.models import (
    Brand,
    Category,
    Collection,
    Product,
    ProductImage,
    ProductVariant,
)
from apps.catalog.services import variant_conflict
from apps.catalog.validators import validate_catalog_image


class CatalogueImageField(forms.ImageField):
    """``ImageField`` that validates content, not the file name."""

    def clean(self, data, initial=None):
        cleaned = super().clean(data, initial)
        if cleaned:
            validate_catalog_image(cleaned)
        return cleaned


class ProductVariantForm(forms.ModelForm):
    """Admin form for one purchasable configuration.

    The product can arrive three ways, and the duplicate check needs it whichever applies:

    * as the hidden ``product`` field Django's inline formset adds automatically;
    * as ``instance.product`` when an existing variant is being edited;
    * as the ``product=`` constructor argument, which is what makes this form testable (and
      reusable) outside an admin inline.

    Without one of them the check silently did nothing on *new* inline rows -- the exact
    case where a duplicate combination is most likely to be typed.
    """

    class Meta:
        model = ProductVariant
        fields = (
            "sku",
            "barcode",
            "color",
            "size",
            "price",
            "compare_at_price",
            "cost_price",
            "is_active",
        )
        widgets = {
            "sku": forms.TextInput(attrs={"autocapitalize": "characters"}),
            "barcode": forms.TextInput(attrs={"autocomplete": "off"}),
            "price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "compare_at_price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "cost_price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    stock_adjustment = forms.IntegerField(
        label=_("Adjust Stock"),
        required=False,
        widget=forms.NumberInput(attrs={"placeholder": "±0", "style": "width: 70px;"}),
        help_text=_("Apply delta (+5, -2) to warehouse on-hand stock."),
    )

    def __init__(self, *args, **kwargs):
        self._product = kwargs.pop("product", None)
        self._user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        # Only active options are selectable, and both are genuinely optional (a belt has a size
        # but no colourway; a gift card has neither).
        self.fields["color"].queryset = self.fields["color"].queryset.filter(is_active=True)
        self.fields["size"].queryset = self.fields["size"].queryset.filter(is_active=True)
        self.fields["color"].required = False
        self.fields["size"].required = False

    @property
    def product(self):
        """The product this variant belongs to, however it was supplied."""
        return self._product or getattr(self.instance, "product", None)

    def clean_sku(self) -> str:
        sku = (self.cleaned_data.get("sku") or "").strip().upper()
        if not sku:
            raise forms.ValidationError(
                _("Give the variant a SKU, or generate one."), code="required"
            )
        clash = ProductVariant.objects.filter(sku=sku)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(
                _("That SKU is already used by another variant."),
                code="duplicate",
            )
        return sku

    def clean(self):
        """Reject a duplicate colour/size pair for this product before the database has to."""
        cleaned = super().clean()
        product = cleaned.get("product") or self.product
        if product is None or not product.pk:
            # Without a product there is no combination to be unique within, and there is nothing
            # to compare against. The database constraint still applies on save.
            return cleaned

        clash = variant_conflict(
            product=product,
            color=cleaned.get("color"),
            size=cleaned.get("size"),
            exclude_pk=self.instance.pk,
        )
        if clash is not None:
            # Blame the option the operator just changed, so the message lands next to the field
            # they need to correct rather than at the top of a long form.
            if self.instance.pk is None or self.instance.color_id != cleaned.get("color"):
                field = "color"
            else:
                field = "size"
            self.add_error(
                field,
                forms.ValidationError(
                    _("This product already has a variant with that colour and size."),
                    code="duplicate_variant",
                ),
            )
        return cleaned


class ProductImageForm(forms.ModelForm):
    """Admin form for one product photograph.

    Supports both local file upload and direct/webpage image URLs.
    Neither is required if the instance already has an image, but at least
    one method must be chosen when adding a new image.
    """

    image = CatalogueImageField(required=False, label=_("Upload image"))
    source_url = forms.URLField(
        required=False,
        assume_scheme="https",
        label=_("Image / Page URL"),
        help_text=_("Direct image URL or supported webpage URL (e.g. iStock, Unsplash)."),
        widget=forms.URLInput(
            attrs={
                "placeholder": "https://example.com/image.jpg or webpage URL",
                "class": "image-url-input",
            }
        ),
    )
    candidate_image_url = forms.URLField(
        required=False,
        assume_scheme="https",
        widget=forms.HiddenInput(),
    )

    class Meta:
        model = ProductImage
        fields = (
            "image",
            "source_url",
            "photographer",
            "attribution",
            "license",
            "alt_text",
            "variant",
            "position",
            "is_primary",
        )
        widgets = {
            "alt_text": forms.TextInput(
                attrs={"placeholder": _("Describe the image for accessibility")}
            ),
            "photographer": forms.TextInput(attrs={"placeholder": _("Photographer or creator")}),
            "attribution": forms.TextInput(attrs={"placeholder": _("Attribution statement")}),
            "license": forms.TextInput(
                attrs={"placeholder": _("License terms (e.g. CC-BY, Unsplash)")}
            ),
        }

    def __init__(self, *args, **kwargs):
        self._product = kwargs.pop("product", None)
        super().__init__(*args, **kwargs)

    @property
    def product(self):
        return self._product or getattr(self.instance, "product", None)

    def clean_alt_text(self) -> str:
        alt_text = (self.cleaned_data.get("alt_text") or "").strip()
        if not alt_text:
            raise forms.ValidationError(
                _("Describe the image, for example 'Black tee, front view'."),
                code="required",
            )
        return alt_text

    def clean(self):
        from apps.catalog.image_fetcher import fetch_and_validate_image_file

        cleaned = super().clean()
        variant = cleaned.get("variant")
        product = cleaned.get("product") or self.product
        image = cleaned.get("image")
        source_url = (cleaned.get("source_url") or "").strip()
        candidate_url = (cleaned.get("candidate_image_url") or "").strip() or None

        if variant is not None and product is not None and variant.product_id != product.pk:
            self.add_error(
                "variant",
                forms.ValidationError(
                    _("That variant belongs to a different product."),
                    code="variant_product_mismatch",
                ),
            )

        # For existing instances that already have a file on disk, image is optional
        has_existing_image = bool(self.instance.pk and self.instance.image)

        if not image and not source_url and not has_existing_image:
            raise forms.ValidationError(_("Provide an image file or an external URL."))

        # If a source URL is provided and no new file was uploaded, fetch and validate from URL
        if source_url and not image:
            try:
                downloaded = fetch_and_validate_image_file(
                    source_url,
                    candidate_url=candidate_url,
                )
                cleaned["image"] = downloaded.file
                if not cleaned.get("photographer") and downloaded.suggested_photographer:
                    cleaned["photographer"] = downloaded.suggested_photographer
                if not cleaned.get("attribution") and downloaded.suggested_attribution:
                    cleaned["attribution"] = downloaded.suggested_attribution
            except forms.ValidationError as error:
                self.add_error("source_url", error)
            except Exception as error:
                self.add_error(
                    "source_url",
                    forms.ValidationError(
                        _("Could not fetch image from URL: %(error)s"),
                        params={"error": str(error)},
                        code="fetch_error",
                    ),
                )

        return cleaned


class CollectionAdminForm(forms.ModelForm):
    """Admin form for Collection with support for direct/webpage image URLs."""

    hero_image = CatalogueImageField(required=False, label=_("Hero image"))
    hero_image_url = forms.URLField(
        required=False,
        assume_scheme="https",
        label=_("Hero image URL"),
        help_text=_("Direct image URL or supported webpage URL for hero banner."),
    )
    banner_image = CatalogueImageField(required=False, label=_("Banner image"))
    banner_image_url = forms.URLField(
        required=False,
        assume_scheme="https",
        label=_("Banner image URL"),
        help_text=_("Direct image URL or supported webpage URL for banner."),
    )

    class Meta:
        from apps.catalog.models import Collection

        model = Collection
        fields = (
            "name",
            "slug",
            "description",
            "is_active",
            "starts_at",
            "ends_at",
            "hero_image",
            "banner_image",
        )

    def clean(self):
        from apps.catalog.image_fetcher import fetch_and_validate_image_file

        cleaned = super().clean()
        hero_url = (cleaned.get("hero_image_url") or "").strip()
        banner_url = (cleaned.get("banner_image_url") or "").strip()

        if hero_url and not cleaned.get("hero_image"):
            try:
                downloaded = fetch_and_validate_image_file(hero_url)
                cleaned["hero_image"] = downloaded.file
            except forms.ValidationError as error:
                self.add_error("hero_image_url", error)

        if banner_url and not cleaned.get("banner_image"):
            try:
                downloaded = fetch_and_validate_image_file(banner_url)
                cleaned["banner_image"] = downloaded.file
            except forms.ValidationError as error:
                self.add_error("banner_image_url", error)

        return cleaned


class CategoryAdminForm(forms.ModelForm):
    """Admin form for Category with support for image URLs."""

    image = CatalogueImageField(required=False, label=_("Image"))
    image_url = forms.URLField(
        required=False,
        assume_scheme="https",
        label=_("Image URL"),
        help_text=_("Direct image URL or supported webpage URL."),
    )

    class Meta:
        from apps.catalog.models import Category

        model = Category
        fields = (
            "name",
            "slug",
            "parent",
            "description",
            "image",
            "display_order",
            "is_active",
        )

    def clean(self):
        from apps.catalog.image_fetcher import fetch_and_validate_image_file

        cleaned = super().clean()
        image_url = (cleaned.get("image_url") or "").strip()
        if image_url and not cleaned.get("image"):
            try:
                downloaded = fetch_and_validate_image_file(image_url)
                cleaned["image"] = downloaded.file
            except forms.ValidationError as error:
                self.add_error("image_url", error)
        return cleaned


class BrandAdminForm(forms.ModelForm):
    """Admin form for Brand with support for logo URLs."""

    logo = CatalogueImageField(required=False, label=_("Logo"))
    logo_url = forms.URLField(
        required=False,
        assume_scheme="https",
        label=_("Logo URL"),
        help_text=_("Direct image URL or supported webpage URL for brand logo."),
    )

    class Meta:
        from apps.catalog.models import Brand

        model = Brand
        fields = (
            "name",
            "slug",
            "website_url",
            "logo",
            "description",
            "display_order",
            "is_active",
        )

    def clean(self):
        from apps.catalog.image_fetcher import fetch_and_validate_image_file

        cleaned = super().clean()
        logo_url = (cleaned.get("logo_url") or "").strip()
        if logo_url and not cleaned.get("logo"):
            try:
                downloaded = fetch_and_validate_image_file(logo_url)
                cleaned["logo"] = downloaded.file
            except forms.ValidationError as error:
                self.add_error("logo_url", error)
        return cleaned




class ProductAdminForm(forms.ModelForm):
    """Admin form for the product itself.

    ``fields`` is spelled out rather than ``"__all__"`` on purpose: a new model field then has to be
    added here deliberately, instead of silently appearing on a merchandising form where nobody
    chose to put it.

    ``slug`` is optional. Django's ``prepopulated_fields`` fills it in the browser, but a slug left
    blank must resolve server-side rather than fail validation -- "the operator did not type a URL"
    is not an error worth blocking a save over.

    Status choices are all offered; the cross-field rule that a product cannot be published into an
    inactive category is enforced by ``Product.clean()`` and reported against the category field.
    """

    class Meta:
        model = Product
        fields = (
            "name",
            "slug",
            "short_description",
            "description",
            "category",
            "brand",
            "fit",
            "materials",
            "tags",
            "collections",
            "status",
            "is_featured",
            "is_new",
            "seo_title",
            "seo_description",
        )
        widgets = {
            "short_description": forms.TextInput(attrs={"maxlength": 300}),
            "description": forms.Textarea(attrs={"rows": 8}),
            "seo_title": forms.TextInput(attrs={"maxlength": 70}),
            "seo_description": forms.TextInput(attrs={"maxlength": 160}),
        }
        filter_horizontal = ("collections", "materials", "tags")
        autocomplete_fields = ("brand", "category", "fit")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ``required = False`` on the field: without it the form is invalid before ``clean_slug``
        # ever runs, so the "generate one for me" path could never be reached.
        self.fields["slug"].required = False

    def clean_slug(self) -> str:
        """Reuse the app's slug policy so the admin matches the API and the storefront.

        The name is read from ``cleaned_data``, not from ``self.instance``: field cleaning runs
        *before* Django copies data onto the instance, so an unsaved product's ``instance.name`` is
        still empty here and a blank slug would resolve to the literal string "item".

        ``ModelForm`` validation would otherwise reject a duplicate with the raw unique-index
        message before the model ever gets the chance to disambiguate it.
        """
        from apps.catalog.slugs import slug_candidate, unique_slug

        name = self.cleaned_data.get("name") or self.instance.name or ""
        slug = slug_candidate(self.cleaned_data.get("slug") or name)
        if not slug:
            raise forms.ValidationError(_("Give the product a name so a URL can be generated."))
        resolved = unique_slug(Product, slug, instance=self.instance, max_length=200)
        if resolved != slug:
            raise forms.ValidationError(
                _("That URL is taken. The available URL is %(slug)s."),
                code="duplicate",
                params={"slug": resolved},
            )
        return resolved


# --------------------------------------------------------------------------------------
# Bulk Action Forms for Admin Operations
# --------------------------------------------------------------------------------------


class BulkAssignCategoryForm(forms.Form):
    """Form for bulk-reassigning category across selected products."""

    category = forms.ModelChoiceField(
        queryset=Category.objects.all(),
        required=True,
        label=_("Target Category"),
        help_text=_("Select the new category to assign to all selected products."),
    )


class BulkAssignBrandForm(forms.Form):
    """Form for bulk-assigning or clearing brand."""

    brand = forms.ModelChoiceField(
        queryset=Brand.objects.all(),
        required=False,
        label=_("Target Brand"),
        help_text=_("Select brand, or leave empty to clear external brand."),
    )


class BulkAssignCollectionForm(forms.Form):
    """Form for bulk adding or removing products to/from a collection."""

    collection = forms.ModelChoiceField(
        queryset=Collection.objects.all(),
        required=True,
        label=_("Target Collection"),
    )
    action_type = forms.ChoiceField(
        choices=[("add", _("Add to collection")), ("remove", _("Remove from collection"))],
        required=True,
        label=_("Action"),
    )


class BulkUpdateTagsForm(forms.Form):
    """Form for bulk tagging products."""

    tags = forms.CharField(
        required=True,
        label=_("Tags to add"),
        help_text=_("Enter comma-separated tag names (e.g. 'summer, trending, oversized')."),
    )


class BulkArchiveConfirmForm(forms.Form):
    """Confirmation form for destructive archiving bulk action."""

    confirm = forms.BooleanField(
        required=True,
        label=_(
            "I understand this will retire and hide all selected products from the storefront."
        ),
    )

