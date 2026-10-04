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

from apps.catalog.models import Product, ProductImage, ProductVariant
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

    def __init__(self, *args, **kwargs):
        self._product = kwargs.pop("product", None)
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
    """Admin form for one photograph.

    ``image`` and ``alt_text`` are both required. Alt text is not decoration: it is what a screen
    reader announces and what a customer sees when the image fails to load, and "0" or "IMG_4021"
    is not a description.
    """

    image = CatalogueImageField()

    class Meta:
        model = ProductImage
        fields = ("image", "alt_text", "variant", "position", "is_primary")

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
        cleaned = super().clean()
        variant = cleaned.get("variant")
        product = cleaned.get("product") or self.product
        if variant is not None and product is not None and variant.product_id != product.pk:
            self.add_error(
                "variant",
                forms.ValidationError(
                    _("That variant belongs to a different product."),
                    code="variant_product_mismatch",
                ),
            )
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
