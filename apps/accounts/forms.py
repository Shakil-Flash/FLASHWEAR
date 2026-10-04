"""Account forms.

Each form owns one screen's input and validation; views stay thin. Where a rule is not
obvious from the field list it is spelled out in the field's ``help_text`` or a comment --
these messages are customer-facing and are translated.
"""

from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Address

User = get_user_model()

AVATAR_EXTENSIONS = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "WEBP": {".webp"},
}

# Input styling lives here rather than in the templates so there is one definition of it and the
# markup stays readable. ``templates/account/_field.html`` renders the widget as-is.
INPUT_CLASSES = (
    "mt-2 block w-full rounded-xl border border-slate-300 px-3 py-2.5 text-slate-900 "
    "shadow-soft transition placeholder:text-slate-400 focus:border-flash-500 focus:outline-none "
    "focus:ring-2 focus:ring-flash-500/20"
)
CHECKBOX_CLASSES = "h-5 w-5 rounded border-slate-300 text-flash-600 focus:ring-flash-500/30"


def input_attrs(**extra) -> dict:
    """Merge ``extra`` over the shared input classes."""
    return {**extra, "class": INPUT_CLASSES}


def checkbox_attrs(**extra) -> dict:
    return {**extra, "class": CHECKBOX_CLASSES}


def _extension(filename: str) -> str:
    name = (filename or "").strip().lower()
    return name[name.rfind(".") :] if "." in name else ""


class RegistrationForm(forms.Form):
    """Create an account.

    Deliberately a ``Form``, not a ``ModelForm``: the row is written by
    :func:`apps.accounts.services.create_account` inside a transaction, so a validation
    failure cannot leave a half-created user behind.

    No marketing checkbox lives here. Consent asked for on a sign-up screen is consent nobody
    reads; marketing preferences are an explicit, revisitable choice on the profile page.
    """

    email = forms.EmailField(
        label=_("Email"),
        max_length=254,
        widget=forms.EmailInput(
            attrs=input_attrs(
                autocomplete="email",
                autocapitalize="none",
                spellcheck="false",
                placeholder="you@example.com",
            )
        ),
        help_text=_("We use this to sign you in and to send order updates."),
    )
    first_name = forms.CharField(
        label=_("First name"),
        max_length=150,
        widget=forms.TextInput(attrs=input_attrs(autocomplete="given-name")),
    )
    last_name = forms.CharField(
        label=_("Last name"),
        max_length=150,
        widget=forms.TextInput(attrs=input_attrs(autocomplete="family-name")),
    )
    password1 = forms.CharField(
        label=_("Password"),
        strip=False,
        widget=forms.PasswordInput(attrs=input_attrs(autocomplete="new-password")),
        help_text=_("At least 8 characters, and not one of the most common passwords."),
    )
    password2 = forms.CharField(
        label=_("Confirm password"),
        strip=False,
        widget=forms.PasswordInput(attrs=input_attrs(autocomplete="new-password")),
    )

    def clean_email(self) -> str:
        email = User.normalize_email_value(self.cleaned_data["email"])
        if User.objects.filter(email=email).exists():
            # Deliberately identical in tone to a validation error rather than "this account
            # already exists": see the docstring above.
            raise ValidationError(
                _("An account with this email address already exists. Try signing in instead."),
                code="duplicate_email",
            )
        return email

    def clean_password2(self) -> str:
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise ValidationError(_("The two password fields did not match."), code="mismatch")
        return password2

    def _post_clean(self):
        # Run Django's configured validators against both attributes so the
        # UserAttributeSimilarityValidator sees the name the customer just typed.
        super()._post_clean()
        password = self.cleaned_data.get("password2")
        if password:
            candidate = User(
                email=self.cleaned_data.get("email", ""),
                first_name=self.cleaned_data.get("first_name", ""),
                last_name=self.cleaned_data.get("last_name", ""),
            )
            try:
                password_validation.validate_password(password, candidate)
            except ValidationError as error:
                self.add_error("password2", error)

    def save(self) -> User:
        return create_user_from_form(self)


def create_user_from_form(form: RegistrationForm) -> User:
    """Persist the validated registration.

    Imported lazily to keep the form module import-safe during app loading.
    """
    from apps.accounts.services import create_account

    return create_account(
        email=form.cleaned_data["email"],
        password=form.cleaned_data["password2"],
        first_name=form.cleaned_data["first_name"],
        last_name=form.cleaned_data["last_name"],
    )


class ProfileForm(forms.Form):
    """Edit identity, contact details and preferences.

    Name fields live on ``User``; everything else on ``Profile``. The form writes both rows in
    one transaction so a customer never ends up with a saved phone number and an unsaved name.
    """

    first_name = forms.CharField(
        label=_("First name"),
        max_length=150,
        widget=forms.TextInput(attrs=input_attrs(autocomplete="given-name")),
    )
    last_name = forms.CharField(
        label=_("Last name"),
        max_length=150,
        widget=forms.TextInput(attrs=input_attrs(autocomplete="family-name")),
    )
    avatar = forms.ImageField(
        label=_("Profile photo"),
        required=False,
        widget=forms.ClearableFileInput(
            attrs=input_attrs(accept="image/png,image/jpeg,image/webp")
        ),
        help_text=_("PNG, JPEG or WebP. Optional."),
    )
    remove_avatar = forms.BooleanField(
        label=_("Remove current photo"),
        required=False,
        widget=forms.CheckboxInput(attrs=checkbox_attrs()),
    )
    phone = forms.CharField(
        label=_("Phone"),
        max_length=32,
        required=False,
        widget=forms.TextInput(
            attrs=input_attrs(autocomplete="tel", inputmode="tel", placeholder="+44 7700 900000")
        ),
        help_text=_("Used for delivery updates only."),
    )
    date_of_birth = forms.DateField(
        label=_("Date of birth"),
        required=False,
        # ``type="date"`` gives a native picker and mobile keyboard; ``format`` makes the value
        # round-trip through HTML5 date strings.
        widget=forms.DateInput(attrs=input_attrs(type="date"), format="%Y-%m-%d"),
        help_text=_("Optional. Used for age-appropriate sizing."),
    )
    preferred_language = forms.ChoiceField(
        label=_("Language"),
        choices=(),
        widget=forms.Select(attrs=input_attrs()),
        help_text=_("Language used for storefront copy and emails."),
    )
    preferred_currency = forms.ChoiceField(
        label=_("Currency"),
        choices=(),
        widget=forms.Select(attrs=input_attrs()),
        help_text=_("How prices are displayed. Payment is still charged in the order currency."),
    )
    marketing_email_opt_in = forms.BooleanField(
        label=_("Email me about new drops and offers"),
        required=False,
        widget=forms.CheckboxInput(attrs=checkbox_attrs()),
    )
    marketing_sms_opt_in = forms.BooleanField(
        label=_("Text me about delivery updates"),
        required=False,
        widget=forms.CheckboxInput(attrs=checkbox_attrs()),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["preferred_language"].choices = list(settings.ACCOUNT_LANGUAGES)
        self.fields["preferred_currency"].choices = list(settings.ACCOUNT_CURRENCIES)

    def clean_date_of_birth(self):
        value = self.cleaned_data.get("date_of_birth")
        if value is None:
            return None
        today = timezone.now().date()
        if value > today:
            raise ValidationError(_("Date of birth cannot be in the future."))
        # 120 years: anything older is a typo, and no one shopping fashion is that old.
        if (today - value).days > 120 * 366:
            raise ValidationError(_("Please check the year you entered."))
        return value

    def clean_avatar(self):
        """Validate the upload by content, then by name.

        The order matters. ``ExtensionValidator``-style name checks are trivially bypassed
        (``avatar.jpg`` containing a script), so the file is opened with Pillow first and its
        real format is compared against the allow-list. The extension is only checked afterwards,
        because browsers and CDNs key off it.
        """
        upload = self.cleaned_data.get("avatar")
        if not upload:
            return None
        return validate_avatar(upload)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("marketing_sms_opt_in") and not cleaned.get("phone"):
            # The database enforces the same rule; doing it here gives the customer a field
            # error instead of a 500.
            self.add_error(
                "phone",
                _("Add a phone number first, or turn off text updates."),
            )
        return cleaned

    def save(self):
        """Write both rows and record what changed (never the values)."""
        from apps.accounts.models import Profile
        from apps.accounts.services import record_account_event

        user = self.user
        profile, _created = Profile.objects.get_or_create(user=user)
        changed: list[str] = []

        for field in ("first_name", "last_name"):
            value = self.cleaned_data[field]
            if getattr(user, field) != value:
                setattr(user, field, value)
                changed.append(field)

        for field in (
            "phone",
            "date_of_birth",
            "preferred_language",
            "preferred_currency",
            "marketing_email_opt_in",
            "marketing_sms_opt_in",
        ):
            value = self.cleaned_data[field]
            if getattr(profile, field) != value:
                setattr(profile, field, value)
                changed.append(field)

        avatar = self.cleaned_data.get("avatar")
        if avatar:
            # Assigning the upload hands it to the field, which moves it under ``upload_to`` on
            # save -- dropping it here would validate the image and then quietly discard it.
            profile.avatar = avatar
            changed.append("avatar")
        elif self.cleaned_data.get("remove_avatar") and profile.avatar:
            profile.avatar.delete(save=False)
            profile.avatar = ""
            changed.append("avatar")

        user.save(update_fields=["first_name", "last_name", "updated_at"])
        profile.save()
        record_account_event(user, "profile_updated", metadata={"fields": sorted(changed)})
        return profile


def validate_avatar(upload) -> object:
    """Return ``upload`` if it is a safe, correctly-sized raster image.

    Rejects, in order: an oversized file, an unsupported format, and a file whose declared
    extension contradicts its actual content. SVG is not in the allow-list and never will be
    without a sanitiser -- an SVG is executable markup served from our own origin.
    """
    allowed_formats = set(settings.ACCOUNT_AVATAR_ALLOWED_FORMATS)
    allowed_extensions = {
        extension for fmt in allowed_formats for extension in AVATAR_EXTENSIONS.get(fmt, set())
    }

    max_bytes = settings.ACCOUNT_AVATAR_MAX_BYTES
    if upload.size > max_bytes:
        raise ValidationError(
            _("That image is %(size).1f MB. Keep it under %(limit)d MB."),
            code="too_large",
            params={"size": upload.size / 1024 / 1024, "limit": max_bytes // (1024 * 1024)},
        )

    # Reject on the declared name before spending CPU on a decode, but only after the size
    # check so the customer gets the more useful message first.
    if _extension(upload.name) not in allowed_extensions:
        raise ValidationError(
            _("Unsupported file type. Use PNG, JPEG or WebP."),
            code="bad_extension",
        )

    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - Pillow is a declared dependency
        raise ValidationError(
            _("Image uploads are unavailable right now."),
        ) from error

    try:
        upload.seek(0)
        with Image.open(upload) as image:
            image.verify()  # structural check; decodes nothing fully
        upload.seek(0)
        with Image.open(upload) as image:
            detected = (image.format or "").upper()
            width, height = image.size
    except ValidationError:
        raise
    except Exception as error:
        raise ValidationError(_("That file is not a readable image."), code="unreadable") from error

    if detected not in allowed_formats:
        raise ValidationError(
            _("Unsupported image format (%(format)s). Use PNG, JPEG or WebP."),
            code="bad_format",
            params={"format": detected or "unknown"},
        )

    max_pixels = settings.ACCOUNT_AVATAR_MAX_PIXELS
    if width > max_pixels or height > max_pixels:
        raise ValidationError(
            _("Images must be at most %(limit)d pixels on each side."),
            code="too_large_dimensions",
            params={"limit": max_pixels},
        )

    upload.seek(0)
    return upload


class AddressForm(forms.ModelForm):
    """Create or edit a delivery address."""

    class Meta:
        model = Address
        fields = (
            "full_name",
            "phone",
            "line1",
            "line2",
            "city",
            "region",
            "postal_code",
            "country",
            "address_type",
            "is_default_shipping",
            "is_default_billing",
        )
        widgets = {
            "full_name": forms.TextInput(attrs=input_attrs(autocomplete="name")),
            "phone": forms.TextInput(
                attrs=input_attrs(
                    autocomplete="tel",
                    inputmode="tel",
                    placeholder="+44 7700 900000",
                )
            ),
            "line1": forms.TextInput(attrs=input_attrs(autocomplete="address-line1")),
            "line2": forms.TextInput(attrs=input_attrs(autocomplete="address-line2")),
            "city": forms.TextInput(attrs=input_attrs(autocomplete="address-level2")),
            "region": forms.TextInput(attrs=input_attrs(autocomplete="address-level1")),
            "postal_code": forms.TextInput(attrs=input_attrs(autocomplete="postal-code")),
            "country": forms.TextInput(
                attrs=input_attrs(
                    autocomplete="country",
                    maxlength=2,
                    placeholder="GB",
                    # Normalised to upper case in ``clean_country``; lower-case input would be
                    # rejected by the ISO-3166 check constraint otherwise.
                    style="text-transform:uppercase",
                )
            ),
            "address_type": forms.Select(attrs=input_attrs()),
            "is_default_shipping": forms.CheckboxInput(attrs=checkbox_attrs()),
            "is_default_billing": forms.CheckboxInput(attrs=checkbox_attrs()),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["country"].label = _("Country code")
        self.fields["region"].label = _("District / state / region")
        self.fields["is_default_shipping"].label = _("Use as my default delivery address")
        self.fields["is_default_billing"].label = _("Use as my default billing address")
        if user is not None and not user.addresses.exists():
            # A customer's first address is the only one that can be the default, so make it
            # the obvious choice instead of a checkbox they have to think about.
            self.fields["is_default_shipping"].initial = True

    def clean_country(self) -> str:
        return (self.cleaned_data.get("country") or "").strip().upper()

    def clean(self):
        cleaned = super().clean()
        address_type = cleaned.get("address_type")
        if cleaned.get("is_default_billing") and address_type == "shipping":
            self.add_error(
                "is_default_billing",
                _("This address is for delivery only. Change the type to use it for billing."),
            )
        if (
            self.user is not None
            and not cleaned.get("is_default_shipping")
            and not self.user.addresses.exists()
        ):
            # The very first address is always the delivery default. Deciding that server-side is
            # better than relying on ``initial``, which a POST ignores -- a customer whose only
            # address is not the default would have a checkout that cannot pre-fill it.
            cleaned["is_default_shipping"] = True
        return cleaned


class AccountDeletionForm(forms.Form):
    """Confirm account closure.

    Password confirmation is the whole point: an attacker with a live session (a stolen cookie,
    an unattended browser) must not be able to close someone's account.
    """

    password = forms.CharField(
        label=_("Password"),
        strip=False,
        widget=forms.PasswordInput(attrs=input_attrs(autocomplete="current-password")),
        help_text=_("Confirm with your current password."),
    )
    confirm = forms.BooleanField(
        label=_("I understand my account will be deactivated"),
        required=True,
        widget=forms.CheckboxInput(attrs=checkbox_attrs()),
        error_messages={"required": _("Please confirm you want to deactivate your account.")},
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_password(self) -> str:
        password = self.cleaned_data["password"]
        if self.user is None or not self.user.check_password(password):
            # Same wording as a wrong password anywhere else in the app.
            raise ValidationError(_("Your password is not correct."), code="invalid_password")
        return password


class EmailVerificationResendForm(forms.Form):
    """Explicit confirmation before another verification mail is sent."""

    confirm = forms.BooleanField(
        label=_("Send me a new verification link"),
        required=True,
        widget=forms.CheckboxInput(attrs=checkbox_attrs()),
    )
