"""Engagement admin: review moderation, promotion campaigns, points ledger (Phase 7).

Principles the screens enforce:

* **Moderation moves status, never rows.** The approve/reject/hide actions call the service
  that stamps the audit timestamps; historical reviews survive a change of heart.
* **The ledger is read-only.** ``PointsTransaction`` cannot be added, edited or deleted from
  the admin -- balance corrections go through the "adjust points" action, which writes a
  normal adjustment row (the audit trail is the ledger itself).
* **Promotion form validation** catches what a database constraint cannot say kindly:
  window order, percentage range and sane limits -- before an operator can save a campaign
  that every checkout would reject.
"""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.engagement.forms import ReviewForm
from apps.engagement.models import (
    PointsReservation,
    PointsTransaction,
    Promotion,
    PromotionUsage,
    Review,
)
from apps.engagement.services import loyalty, reviews
from apps.engagement.services.errors import LoyaltyError

__all__ = ["PointsReservationAdmin", "PointsTransactionAdmin", "PromotionAdmin", "ReviewAdmin"]


# =============================================================================
# Reviews
# =============================================================================


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    """Moderation queue: newest pending first, bulk status transitions."""

    list_display = (
        "product",
        "author_email",
        "rating",
        "status",
        "verified_purchase",
        "created_at",
    )
    list_filter = ("status", "rating", "verified_purchase")
    search_fields = ("product__name", "product__slug", "author__email", "title", "body")
    list_select_related = ("product", "author")
    ordering = ("-created_at",)
    actions = ("approve_selected", "reject_selected", "hide_selected")
    form = ReviewForm

    @admin.display(description=_("Author"))
    def author_email(self, obj: Review) -> str:
        return obj.author.email

    def get_readonly_fields(self, request, obj=None):
        # On an existing review the provenance and audit fields are evidence, not inputs;
        # status changes happen through the actions so a misclick cannot rewrite history.
        if obj is not None:
            return (
                "product",
                "author",
                "order",
                "verified_purchase",
                "created_at",
                "published_at",
                "moderated_at",
            )
        return ()

    @admin.action(description=_("Approve selected reviews (publish)"))
    def approve_selected(self, request, queryset):
        count = reviews.moderate(queryset, Review.Status.PUBLISHED)
        messages.success(request, _("%(count)d review(s) published.") % {"count": count})

    @admin.action(description=_("Reject selected reviews"))
    def reject_selected(self, request, queryset):
        count = reviews.moderate(queryset, Review.Status.REJECTED)
        messages.success(request, _("%(count)d review(s) rejected.") % {"count": count})

    @admin.action(description=_("Hide selected reviews (unpublish)"))
    def hide_selected(self, request, queryset):
        count = reviews.moderate(queryset, Review.Status.HIDDEN)
        messages.success(request, _("%(count)d review(s) hidden.") % {"count": count})


# =============================================================================
# Promotions
# =============================================================================


class PromotionAdminForm(forms.ModelForm):
    """Friendly validation on top of the model's constraints."""

    class Meta:
        model = Promotion
        fields = (
            "code",
            "name",
            "description",
            "discount_type",
            "discount_value",
            "starts_at",
            "ends_at",
            "is_active",
            "usage_limit",
            "used_count",
            "per_user_limit",
            "min_order_amount",
            "allow_loyalty_redemption",
        )

    def clean_code(self) -> str:
        from apps.engagement.services.promotions import normalise_code

        code = normalise_code(self.cleaned_data["code"])
        if not code:
            raise forms.ValidationError(_("A promotion needs a code."))
        existing = Promotion.objects.filter(code=code).exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError(_("That code already exists."))
        return code

    def clean(self):
        cleaned = super().clean()
        starts = cleaned.get("starts_at")
        ends = cleaned.get("ends_at")
        if starts and ends and starts >= ends:
            raise forms.ValidationError(_("The end of the window must be after its start."))
        discount_type = cleaned.get("discount_type")
        value = cleaned.get("discount_value")
        if discount_type == Promotion.DiscountType.PERCENTAGE and value is not None and value > 100:
            raise forms.ValidationError(_("A percentage discount cannot exceed 100%."))
        if value is not None and value <= 0:
            raise forms.ValidationError(_("The discount must be greater than zero."))
        return cleaned


class PromotionUsageInline(admin.TabularInline):
    model = PromotionUsage
    extra = 0
    fields = ("user", "order", "discount_amount", "created_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    """Campaign CRUD with usage visible inline; the counter itself is engine-owned."""

    list_display = ("code", "name", "discount_label_display", "window", "usage", "is_active")
    list_filter = ("is_active", "discount_type")
    search_fields = ("code", "name")
    readonly_fields = ("used_count", "created_at", "updated_at")
    inlines = (PromotionUsageInline,)
    date_hierarchy = "starts_at"
    form = PromotionAdminForm

    @admin.display(description=_("Discount"))
    def discount_label_display(self, obj: Promotion) -> str:
        return obj.discount_label

    @admin.display(description=_("Window"))
    def window(self, obj: Promotion) -> str:
        return f"{obj.starts_at:%Y-%m-%d %H:%M} → {obj.ends_at:%Y-%m-%d %H:%M}"

    @admin.display(description=_("Usage"))
    def usage(self, obj: Promotion) -> str:
        if obj.usage_limit is None:
            return _("%(used)d used") % {"used": obj.used_count}
        return _("%(used)d of %(limit)d used") % {
            "used": obj.used_count,
            "limit": obj.usage_limit,
        }


# =============================================================================
# Points ledger and holds
# =============================================================================


class PointsAdjustForm(forms.Form):
    """Signed point change plus the reason that will land in the ledger."""

    amount = forms.IntegerField(
        label=_("Points change"),
        min_value=-1000000,
        max_value=1000000,
        help_text=_("Positive credits the customer, negative takes points back. Sign matters."),
    )
    note = forms.CharField(
        label=_("Note"),
        max_length=200,
        required=False,
        help_text=_("Shown on the ledger row, e.g. 'goodwill after delay'."),
    )


@admin.register(PointsTransaction)
class PointsTransactionAdmin(admin.ModelAdmin):
    """Read-only ledger. Corrections happen via the adjust action, which appends a row."""

    list_display = ("user_email", "amount", "transaction_type", "reference", "created_at")
    list_filter = ("transaction_type",)
    search_fields = ("user__email", "reference", "note")
    list_select_related = ("user",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    actions = ("adjust_selected_users",)
    readonly_fields = [
        "user",
        "amount",
        "transaction_type",
        "reference",
        "expires_at",
        "order",
        "note",
        "created_at",
        "updated_at",
    ]

    @admin.display(description=_("Customer"))
    def user_email(self, obj: PointsTransaction) -> str:
        return obj.user.email

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description=_("Adjust points for customers behind selected rows"))
    def adjust_selected_users(self, request, queryset):
        """Intermediate step: one signed change applied to every selected customer.

        Same shape as the inventory adjust action: the operator states *why*, and the
        service appends the ledger row that explains the balance change.
        """
        form = PointsAdjustForm(request.POST or None)

        if "apply" in request.POST and form.is_valid():
            amount = form.cleaned_data["amount"]
            note = form.cleaned_data["note"]
            user_ids = list(queryset.values_list("user_id", flat=True).order_by().distinct())
            applied = 0
            for user_id in user_ids:
                try:
                    loyalty.adjust_balance(
                        get_user_model().objects.get(pk=user_id),
                        amount,
                        note=note,
                    )
                except LoyaltyError as err:
                    messages.error(request, err.message)
                else:
                    applied += 1
            messages.success(
                request,
                _("Wrote %(count)d adjustment(s) of %(amount)d points.")
                % {"count": applied, "amount": amount},
            )
            return redirect(reverse("admin:engagement_pointstransaction_changelist"))

        context = {
            **self.admin_site.each_context(request),
            "title": _("Adjust FLASH Points"),
            "form": form,
            "queryset": queryset.select_related("user"),
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "media": self.media,
        }
        return TemplateResponse(request, "admin/engagement/points/adjust.html", context)


@admin.register(PointsReservation)
class PointsReservationAdmin(admin.ModelAdmin):
    """Active/expired/consumed holds. Release is the only mutating action."""

    list_display = ("user_email", "points", "status", "checkout", "order", "expires_at")
    list_filter = ("status",)
    search_fields = ("user__email",)
    list_select_related = ("user", "checkout", "order")
    ordering = ("-created_at",)
    actions = ("release_selected",)

    @admin.display(description=_("Customer"))
    def user_email(self, obj: PointsReservation) -> str:
        return obj.user.email

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description=_("Release selected holds"))
    def release_selected(self, request, queryset):
        updated = queryset.filter(status=PointsReservation.Status.ACTIVE).update(
            status=PointsReservation.Status.RELEASED, updated_at=timezone.now()
        )
        messages.success(request, _("%(count)d hold(s) released.") % {"count": updated})
