"""Inventory admin: stock levels, the adjustment workflow and the ledger.

Stock counters are never edited in place. The operator changes them through the
"adjust" action, which goes through :func:`apps.inventory.services.adjust_stock`
and therefore writes the movement that explains the change. The ledger and holds
are read-only screens -- evidence, not inputs.
"""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.inventory import services
from apps.inventory.models import InventoryMovement, Reservation, Stock

__all__ = ["InventoryMovementAdmin", "ReservationAdmin", "StockAdmin"]


class StockAdjustForm(forms.Form):
    """Signed quantity change plus the reason that will land in the ledger."""

    delta = forms.IntegerField(
        label=_("Quantity change"),
        min_value=-100000,
        max_value=100000,
        help_text=_(
            "Positive receives stock, negative writes it off. Absolute numbers, not totals."
        ),
    )
    kind = forms.ChoiceField(
        label=_("Reason"),
        choices=[
            (InventoryMovement.Kind.RECEIVED, _("Received from supplier")),
            (InventoryMovement.Kind.ADJUSTMENT, _("Manual correction")),
        ],
        initial=InventoryMovement.Kind.ADJUSTMENT,
    )
    note = forms.CharField(
        label=_("Note"),
        max_length=200,
        required=False,
        help_text=_("Shown on the movement row, e.g. 'delivery 4711' or 'damaged in transit'."),
    )


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    """Available-at-a-glance list plus the ledger-backed adjustment workflow."""

    list_display = ("variant", "sku", "on_hand", "reserved", "available_display", "updated_at")
    list_select_related = ("variant", "variant__product")
    search_fields = ("variant__sku", "variant__product__name")
    list_filter = ("variant__product__status", "variant__product__category")
    readonly_fields = ("variant", "reserved", "created_at", "updated_at")
    actions = ("adjust_stock",)

    @admin.display(description=_("SKU"), ordering="variant__sku")
    def sku(self, obj: Stock) -> str:
        return obj.variant.sku

    @admin.display(description=_("Available"))
    def available_display(self, obj: Stock) -> int:
        return obj.available

    @admin.action(description=_("Adjust stock for the selected rows"))
    def adjust_stock(self, request, queryset):
        """Intermediate step: one signed change applied to every selected SKU.

        Doing it as an action (rather than an editable column) means the ledger
        entry and the counter move together, and the operator states *why*.
        """
        form = StockAdjustForm(request.POST or None)

        if "apply" in request.POST and form.is_valid():
            delta = form.cleaned_data["delta"]
            kind = form.cleaned_data["kind"]
            note = form.cleaned_data["note"]
            applied = 0
            for stock in queryset.select_related("variant"):
                try:
                    services.adjust_stock(
                        stock.variant,
                        delta,
                        kind=kind,
                        user=request.user,
                        note=note,
                        reference="admin",
                    )
                except (services.InsufficientStock, ValueError) as err:
                    messages.error(request, str(err))
                else:
                    applied += 1
            if applied:
                messages.success(
                    request,
                    _("Adjusted %(count)d stock row(s) by %(delta)d.")
                    % {"count": applied, "delta": delta},
                )
            return redirect(reverse("admin:inventory_stock_changelist"))

        context = {
            **self.admin_site.each_context(request),
            "title": _("Adjust stock"),
            "form": form,
            "queryset": queryset.select_related("variant", "variant__product"),
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "media": self.media,
        }
        return TemplateResponse(request, "admin/inventory/stock/adjust.html", context)


@admin.register(InventoryMovement)
class InventoryMovementAdmin(admin.ModelAdmin):
    """Append-only ledger: nothing here can be created, edited or deleted."""

    list_display = ("created_at", "variant", "kind", "on_hand_delta", "reserved_delta", "reference")
    list_filter = ("kind",)
    search_fields = ("variant__sku", "reference", "note")
    list_select_related = ("variant", "user")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in InventoryMovement._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    """Read-only holds, plus a support action that releases them safely."""

    list_display = ("variant", "quantity", "status", "checkout", "order", "expires_at")
    list_filter = ("status",)
    search_fields = ("variant__sku", "order__number")
    list_select_related = ("variant", "checkout", "order")
    readonly_fields = [f.name for f in Reservation._meta.fields]
    actions = ("release_selected",)

    @admin.action(description=_("Release the selected active holds"))
    def release_selected(self, request, queryset):
        released = services.release_holds(
            queryset.filter(status=Reservation.Status.ACTIVE),
            reference="admin",
            note="Released from the admin.",
        )
        if released:
            messages.success(request, _("Released %(count)d hold(s).") % {"count": released})
        else:
            messages.info(request, _("No active holds among the selection."))
        return redirect(reverse("admin:inventory_reservation_changelist"))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Deleting an active hold would orphan its reserved counter.
        return False
