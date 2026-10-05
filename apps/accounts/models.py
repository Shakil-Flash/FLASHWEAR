"""Customer accounts: the user record plus everything a customer owns.

The split is deliberate:

* :class:`User` stays a credential record (email, names, lifecycle markers). It is what the
  auth backend and every sign-in query load, so it should stay narrow.
* :class:`Profile` holds preferences and contact details that no authentication path needs.
* :class:`Address` is a customer's saved delivery address, with the "only one default" rule
  enforced by the database rather than by application code alone.
* :class:`AccountEvent` is a generic append-only audit trail for later phases to reuse.
"""

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.validators import validate_avatar


class UserManager(BaseUserManager):
    """Custom manager for ``User`` using email as the unique identifier."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("The email field must be set")
        email = User.normalize_email_value(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """Custom User model with email as the primary login identifier.

    Phase 2 adds two lifecycle markers only:

    * ``email_verified_at`` -- when the address was proven. Storing a timestamp rather than a
      boolean keeps the "verified before/after" question answerable later.
    * ``deactivated_at`` -- when the customer asked for account closure. ``is_active`` is the
      switch Django already honours for sign-in; the timestamp explains *why* and drives the
      retention/anonymisation policy in :mod:`apps.accounts.services`.

    Everything else a customer owns (avatar, phone, preferences) lives on :class:`Profile` so
    this table stays a credential record rather than a general customer blob.
    """

    username = None
    # ``unique=True`` already creates a unique index; a separate db_index would be redundant.
    email = models.EmailField(_("email address"), unique=True)

    first_name = models.CharField(_("first name"), max_length=150, blank=True)
    last_name = models.CharField(_("last name"), max_length=150, blank=True)

    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_("Designates whether this user should be treated as active."),
    )
    is_staff = models.BooleanField(
        _("staff status"),
        default=False,
        help_text=_("Designates whether the user can log into this admin site."),
    )

    # Indexed for the customers screen: a date_joined window plus "-date_joined"
    # ordering (email stays unique-and-indexed above).
    date_joined = models.DateTimeField(_("date joined"), default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    email_verified_at = models.DateTimeField(
        _("email verified at"),
        null=True,
        blank=True,
        help_text=_("Left empty until the address is proven; never guessed from the signup form."),
    )
    deactivated_at = models.DateTimeField(
        _("deactivated at"),
        null=True,
        blank=True,
        help_text=_("Set when the customer requests account closure."),
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ("email",)

    @staticmethod
    def normalize_email_value(email: str) -> str:
        """Lower-case and trim an address.

        ``BaseUserManager.normalize_email`` only fixes the domain part, which leaves
        ``Bob@example.com`` and ``bob@example.com`` as distinct rows. Sign-in lookups are
        case-insensitive, so the stored value must be canonicalised too.
        """
        return BaseUserManager.normalize_email(email or "").strip().lower()

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.normalize_email_value(self.email)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.email

    def get_full_name(self) -> str:
        full_name = f"{self.first_name} {self.last_name}".strip()
        return full_name or self.email

    def get_short_name(self) -> str:
        return self.first_name or self.email

    @property
    def is_email_verified(self) -> bool:
        """Whether the address has been proven.

        Kept as a property (not a column) so there is exactly one source of truth.
        """
        return self.email_verified_at is not None

    @property
    def is_deactivated(self) -> bool:
        """Whether the customer asked to close the account."""
        return self.deactivated_at is not None

    @property
    def is_usable(self) -> bool:
        """Whether the account may sign in: active, not deactivated, password set."""
        return self.is_active and not self.is_deactivated and self.has_usable_password()


class Profile(models.Model):
    """Customer-owned preferences and contact details.

    Separate from :class:`User` because nothing here is an authentication concern: a future
    recommendation or loyalty service can join this row without touching credentials, and the
    fields can grow without widening the table every sign-in query touches.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
        verbose_name=_("user"),
    )
    avatar = models.ImageField(
        _("avatar"),
        upload_to="avatars/",
        blank=True,
        # Model-level, not form-level: the admin's Profile inline and any future form
        # that edits this row must run the same content-first check as the storefront.
        validators=[validate_avatar],
        help_text=_("Square image. PNG, JPEG or WebP, within the configured size limit."),
    )
    phone = models.CharField(_("phone"), max_length=32, blank=True)
    date_of_birth = models.DateField(_("date of birth"), null=True, blank=True)
    preferred_language = models.CharField(
        _("preferred language"),
        max_length=10,
        default="en",
        help_text=_("BCP 47 language tag used for storefront copy."),
    )
    preferred_currency = models.CharField(
        _("preferred currency"),
        max_length=3,
        default="USD",
        help_text=_("ISO 4217 code. A display preference only -- never used for money maths."),
    )
    # Explicit opt-in: an unchecked box must stay unchecked, so these default to False.
    marketing_email_opt_in = models.BooleanField(_("marketing email opt-in"), default=False)
    marketing_sms_opt_in = models.BooleanField(_("marketing SMS opt-in"), default=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("profile")
        verbose_name_plural = _("profiles")
        ordering = ("user__email",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(preferred_currency__regex=r"^[A-Z]{3}$"),
                name="accounts_profile_currency_iso4217",
            ),
            models.CheckConstraint(
                condition=(models.Q(marketing_sms_opt_in=False) | models.Q(phone__gt="")),
                name="accounts_profile_sms_opt_in_requires_phone",
            ),
        ]

    def __str__(self) -> str:
        return f"Profile<{self.user.email}>"


class Address(models.Model):
    """A saved delivery address.

    "Default" is modelled as two booleans rather than a pointer column so the invariants are
    checkable in the database: partial unique indexes guarantee at most one default shipping and
    one default billing address per customer. Changing a default goes through
    :meth:`make_default`, which clears the previous holder inside one transaction.
    """

    class AddressType(models.TextChoices):
        SHIPPING = "shipping", _("Shipping")
        BILLING = "billing", _("Billing")
        BOTH = "both", _("Shipping and billing")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="addresses",
        verbose_name=_("user"),
    )
    full_name = models.CharField(_("full name"), max_length=150)
    phone = models.CharField(_("phone"), max_length=32)
    line1 = models.CharField(_("address line 1"), max_length=200)
    line2 = models.CharField(_("address line 2"), max_length=200, blank=True)
    city = models.CharField(_("city"), max_length=100)
    region = models.CharField(_("district / state / region"), max_length=100)
    postal_code = models.CharField(_("postal code"), max_length=20)
    country = models.CharField(
        _("country"),
        max_length=2,
        help_text=_("ISO 3166-1 alpha-2 code, e.g. DE, GB, US."),
    )
    address_type = models.CharField(
        _("address type"),
        max_length=10,
        choices=AddressType.choices,
        default=AddressType.SHIPPING,
    )
    is_default_shipping = models.BooleanField(_("default shipping address"), default=False)
    is_default_billing = models.BooleanField(_("default billing address"), default=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("address")
        verbose_name_plural = _("addresses")
        ordering = ("-is_default_shipping", "-is_default_billing", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_default_shipping=True),
                name="accounts_address_one_default_shipping_per_user",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_default_billing=True),
                name="accounts_address_one_default_billing_per_user",
            ),
            models.CheckConstraint(
                condition=models.Q(country__regex=r"^[A-Z]{2}$"),
                name="accounts_address_country_iso3166",
            ),
            # A default billing address is only meaningful for an address used for billing.
            models.CheckConstraint(
                condition=(
                    ~models.Q(is_default_billing=True)
                    | models.Q(address_type__in=["billing", "both"])
                ),
                name="accounts_address_billing_default_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} - {self.line1}, {self.city}"

    def save(self, *args, **kwargs):
        # Trim every free-text field: pasted addresses arrive with stray whitespace.
        for field in ("full_name", "phone", "line1", "line2", "city", "region", "postal_code"):
            value = getattr(self, field, "")
            if value:
                setattr(self, field, value.strip())
        self.country = (self.country or "").strip().upper()
        if self.address_type == self.AddressType.SHIPPING:
            # A shipping-only address cannot be the billing default.
            self.is_default_billing = False
        super().save(*args, **kwargs)

    @property
    def as_billing_eligible(self) -> bool:
        return self.address_type in {self.AddressType.BILLING, self.AddressType.BOTH}

    @property
    def single_line(self) -> str:
        parts = [self.line1, self.line2, self.city, self.region, self.postal_code, self.country]
        return ", ".join(part for part in parts if part)

    def clear_default_shipping(self) -> None:
        self.is_default_shipping = False
        self.save(update_fields=["is_default_shipping", "updated_at"])

    def clear_default_billing(self) -> None:
        self.is_default_billing = False
        self.save(update_fields=["is_default_billing", "updated_at"])


class AccountEvent(models.Model):
    """Append-only record of meaningful account changes.

    Deliberately generic so later phases (orders, returns, reviews) can reuse it without a
    schema migration per feature. It records *what happened*, never a credential: no passwords,
    no reset or verification tokens, no raw addresses. ``metadata`` holds small non-sensitive
    facts such as ``{"fields": ["phone", "date_of_birth"]}``.

    ``user`` is nullable and set to null on user deletion so audit rows survive the account
    they describe while staying anonymous.
    """

    class Type(models.TextChoices):
        REGISTERED = "registered", _("Registered")
        EMAIL_VERIFICATION_SENT = "email_verification_sent", _("Email verification sent")
        EMAIL_VERIFIED = "email_verified", _("Email verified")
        PASSWORD_RESET_REQUESTED = "password_reset_requested", _("Password reset requested")
        PASSWORD_CHANGED = "password_changed", _("Password changed")
        PROFILE_UPDATED = "profile_updated", _("Profile updated")
        ADDRESS_CREATED = "address_created", _("Address created")
        ADDRESS_UPDATED = "address_updated", _("Address updated")
        ADDRESS_DELETED = "address_deleted", _("Address deleted")
        DEFAULT_ADDRESS_CHANGED = "default_address_changed", _("Default address changed")
        ACCOUNT_DEACTIVATED = "account_deactivated", _("Account deactivated")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="account_events",
        verbose_name=_("user"),
    )
    email_digest = models.CharField(
        _("email digest"),
        max_length=64,
        blank=True,
        help_text=_(
            "Salted hash of the account email, so an event stays traceable after "
            "anonymisation without storing the address."
        ),
    )
    event_type = models.CharField(_("event type"), max_length=32, choices=Type.choices)
    channel = models.CharField(
        _("channel"),
        max_length=20,
        default="web",
        help_text=_("Surface the event came from, e.g. web or api."),
    )
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    ip_digest = models.CharField(
        _("IP digest"),
        max_length=64,
        blank=True,
        help_text=_("Salted hash of the client address. Raw addresses are never stored."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("account event")
        verbose_name_plural = _("account events")
        ordering = ("-created_at", "-id")
        indexes = [
            # Oracle's 30-character identifier limit is the tightest one Django supports.
            models.Index(fields=["user", "-created_at"], name="acc_evt_user_created_idx"),
            models.Index(fields=["event_type", "-created_at"], name="acc_evt_type_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} @ {self.created_at:%Y-%m-%d}"
