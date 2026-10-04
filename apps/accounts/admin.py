"""Admin for the account models.

The customer's email is displayed in full here on purpose: this is staff-only, behind
``is_staff``, and support needs to be able to find an account by address. That is exactly why
``AccountEvent`` stores a digest instead -- the audit trail is readable by a wider group than
this page.
"""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import AccountEvent, Address, Profile

User = get_user_model()


class ProfileInline(admin.StackedInline):
    """Preferences live with the user in the admin; there is no separate menu to forget."""

    model = Profile
    can_delete = False
    verbose_name_plural = _("Profile")
    fields = (
        "avatar",
        "phone",
        "date_of_birth",
        "preferred_language",
        "preferred_currency",
        "marketing_email_opt_in",
        "marketing_sms_opt_in",
        "created_at",
        "updated_at",
    )
    readonly_fields = ("created_at", "updated_at")


class AddressInline(admin.TabularInline):
    """Addresses are edited inline; they are only ever seen in the context of their owner."""

    model = Address
    extra = 0
    fields = (
        "full_name",
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
    readonly_fields = ("created_at", "updated_at")


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Admin for the custom ``User`` model (email-based)."""

    ordering = ("email",)
    list_display = (
        "email",
        "first_name",
        "last_name",
        "verification_badge",
        "is_active",
        "is_staff",
        "is_superuser",
        "date_joined",
    )
    list_filter = ("is_active", "is_staff", "is_superuser", "email_verified_at", "deactivated_at")
    search_fields = ("email", "first_name", "last_name")
    readonly_fields = (
        "date_joined",
        "updated_at",
        "last_login",
        "email_verified_at",
        "deactivated_at",
    )
    inlines = (ProfileInline, AddressInline)

    @admin.display(description=_("email"), ordering="email")
    def verification_badge(self, obj: User) -> str:
        """A plain-text verified/unverified marker.

        Text rather than a coloured badge: staff scan this column in bulk, and colour alone is
        unreadable to anyone relying on a screen reader or working in a dark terminal.
        """
        if not obj.is_email_verified:
            return _("unverified")
        return _("verified")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name")}),
        # Phone, date of birth, language, currency and consent are ``Profile`` columns, so they are
        # edited in ``ProfileInline`` above. Repeating their names here would ask the admin for
        # fields this model does not have.
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (
            _("Important dates"),
            {
                "fields": (
                    "last_login",
                    "date_joined",
                    "updated_at",
                    "email_verified_at",
                    "deactivated_at",
                )
            },
        ),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "password1",
                    "password2",
                    "first_name",
                    "last_name",
                    "is_staff",
                    "is_superuser",
                    "is_active",
                ),
            },
        ),
    )

    filter_horizontal = ("groups", "user_permissions")

    def get_queryset(self, request):
        # ``select_related`` avoids one query per row for the inline previews in the changelist.
        return super().get_queryset(request).select_related("profile")


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    """Standalone address admin, for support lookups across customers."""

    list_display = (
        "full_name",
        "customer",
        "city",
        "country",
        "address_type",
        "is_default_shipping",
        "is_default_billing",
        "created_at",
    )
    list_filter = ("country", "address_type", "is_default_shipping", "is_default_billing")
    search_fields = ("full_name", "user__email", "line1", "city", "postal_code")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("user",)
    list_select_related = ("user",)

    @admin.display(description=_("customer"), ordering="user__email")
    def customer(self, obj: Address) -> str:
        """Named ``customer`` rather than ``user`` so it does not shadow the FK field."""
        return obj.user.email


@admin.register(AccountEvent)
class AccountEventAdmin(admin.ModelAdmin):
    """Read-only audit trail.

    Deliberately append-only: there is no add form and no change permission. If an operator needs
    to correct an entry, the honest fix is a new row explaining the correction -- an editable audit
    log is not an audit log.
    """

    list_display = (
        "created_at",
        "event_type",
        "user",
        "channel",
        "email_digest",
        "ip_digest",
        "summary",
    )
    list_filter = ("event_type", "channel", "created_at")
    search_fields = ("email_digest", "ip_digest", "user__email")
    readonly_fields = (
        "user",
        "event_type",
        "channel",
        "metadata",
        "email_digest",
        "ip_digest",
        "created_at",
    )
    date_hierarchy = "created_at"
    list_select_related = ("user",)

    @admin.display(description=_("detail"))
    def summary(self, obj: AccountEvent) -> str:
        """Render ``metadata`` as a short string.

        ``metadata`` is operator-authored JSON, so it is escaped here rather than marked safe.
        """
        if not obj.metadata:
            return "-"
        return format_html("{}", ", ".join(f"{key}={value}" for key, value in obj.metadata.items()))

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # Deleting audit rows is not a permission anyone needs.
        return False


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    """Preferences, reachable directly for a support search by phone number."""

    list_display = (
        "customer",
        "phone",
        "preferred_language",
        "preferred_currency",
        "marketing_email_opt_in",
        "marketing_sms_opt_in",
    )
    list_filter = (
        "preferred_currency",
        "preferred_language",
        "marketing_email_opt_in",
        "marketing_sms_opt_in",
    )
    search_fields = ("user__email", "phone")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("user",)
    list_select_related = ("user",)

    @admin.display(description=_("customer"), ordering="user__email")
    def customer(self, obj: Profile) -> str:
        """Named ``customer`` rather than ``user`` so it does not shadow the FK field."""
        return obj.user.email
