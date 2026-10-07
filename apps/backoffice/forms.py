"""Back Office forms (Phase 16).

Forms here **choose arguments**, they do not validate business rules: a delta may not be
zero because :func:`apps.inventory.services.adjust_stock` refuses a zero delta, and a
ticket status may be impossible because ``apps.support`` owns that graph. What a form does
own is shape -- integer fields that must be integers, choices drawn from a model's own
``TextChoices``, and a required reason for anything that changes money or stock.

Every write form posts to a view that is gated by a capability *and* runs the domain
service, so a hand-crafted POST reaches the same validation as a button click.
"""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _

from apps.backoffice.permissions import BACKOFFICE_GROUPS
from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryMovement
from apps.support.models import SupportTicket


class _LabelledModelChoiceField(forms.ModelChoiceField):
    """Show an email (or SKU) instead of a generic object repr."""

    def label_from_instance(self, obj):
        return getattr(obj, "email", None) or str(obj)


_INPUT = (
    "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 "
    "placeholder-slate-400 focus:border-slate-900 focus:outline-none "
    "dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500 "
    "dark:focus:border-slate-100"
)
_SELECT = (
    "rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 "
    "focus:border-slate-900 focus:outline-none "
    "dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 "
    "dark:focus:border-slate-100"
)
_TEXTAREA = _INPUT


class StyledForm(forms.Form):
    """Give every widget the same one class of styling.

    Set in ``__init__`` rather than repeated as ``widget=forms.TextInput(attrs=...)`` on
    twenty fields: the layout is one decision, so it is written once, and a new form gets
    it by inheriting rather than by remembering.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", _SELECT)
            elif isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("class", _TEXTAREA)
            elif hasattr(widget, "input_type"):
                widget.attrs.setdefault("class", _INPUT)


class InventoryAdjustForm(StyledForm):
    """One signed stock movement: the SKU, how much, why, and who for the ledger."""

    variant = _LabelledModelChoiceField(
        queryset=ProductVariant.objects.select_related("product").order_by("sku"),
        label=_("Variant"),
    )
    delta = forms.IntegerField(
        label=_("Change"),
        min_value=-10_000,
        max_value=10_000,
        help_text=_("Signed units to add (positive) or remove (negative)."),
    )
    kind = forms.ChoiceField(
        label=_("Movement type"),
        choices=[
            (InventoryMovement.Kind.RECEIVED, _("Received stock")),
            (InventoryMovement.Kind.ADJUSTMENT, _("Manual adjustment")),
        ],
        initial=InventoryMovement.Kind.ADJUSTMENT,
    )
    note = forms.CharField(label=_("Note"), max_length=200, required=False, widget=forms.TextInput)
    reason = forms.CharField(label=_("Reason"), max_length=255)


class PointsAdjustForm(StyledForm):
    """A manual FLASH Points credit or debit, with the reason that lands in the ledger."""

    user = _LabelledModelChoiceField(
        queryset=get_user_model().objects.filter(is_active=True).order_by("email"),
        label=_("Customer"),
    )
    amount = forms.IntegerField(
        label=_("Points"),
        min_value=-1_000_000,
        max_value=1_000_000,
        help_text=_("Positive credits the account, negative debits it."),
    )
    reason = forms.CharField(label=_("Reason"), max_length=255)


class OrderActionForm(StyledForm):
    """Advance or cancel an order; the transition itself is chosen by the service."""

    action = forms.ChoiceField(
        label=_("Action"),
        choices=[
            ("processing", _("Mark processing")),
            ("ship", _("Mark shipped")),
            ("deliver", _("Mark delivered")),
            ("cancel", _("Cancel order")),
        ],
    )
    carrier = forms.CharField(
        label=_("Carrier"), max_length=64, required=False, widget=forms.TextInput
    )
    tracking_number = forms.CharField(label=_("Tracking number"), max_length=64, required=False)
    note = forms.CharField(label=_("Note"), max_length=255, required=False)


class OrderNoteForm(StyledForm):
    """Append an internal note to an order's timeline (no state change)."""

    note = forms.CharField(label=_("Note"), max_length=500, widget=forms.Textarea)


class ReviewModerationForm(StyledForm):
    """Bulk review decision: one status for the selection, one reason for the log."""

    status = forms.ChoiceField(label=_("Decision"))
    reason = forms.CharField(label=_("Reason"), max_length=255)

    def __init__(self, *args, **kwargs):
        from apps.engagement.models import Review

        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [(value, label) for value, label in Review.Status.choices]


class CatalogBulkForm(StyledForm):
    """Bulk catalogue state change over the selected products."""

    action = forms.ChoiceField(
        label=_("Action"),
        choices=[
            ("publish", _("Publish")),
            ("unpublish", _("Return to draft")),
            ("archive", _("Archive")),
        ],
    )
    reason = forms.CharField(label=_("Reason"), max_length=255, required=False)


class LoopActionForm(StyledForm):
    """One FLASH Loop decision for the selected item."""

    action = forms.ChoiceField(
        label=_("Decision"),
        choices=[
            ("start_review", _("Start review")),
            ("approve", _("Approve resale")),
            ("reject", _("Reject item")),
            ("publish", _("Publish listing")),
            ("accept_recycling", _("Accept recycling")),
            ("reject_recycling", _("Reject recycling")),
            ("decline_trade_in", _("Decline trade-in")),
        ],
    )
    authenticity = forms.ChoiceField(
        label=_("Authenticity"),
        choices=[("", "—"), ("authentic", _("Authentic")), ("flagged", _("Flagged"))],
        required=False,
    )
    note = forms.CharField(label=_("Note"), max_length=255, required=False)
    reason = forms.CharField(label=_("Reason"), max_length=255, required=False)


class SupportAssignForm(StyledForm):
    """Hand a ticket to a member of the desk (or release it with an empty agent)."""

    agent = _LabelledModelChoiceField(
        queryset=get_user_model().objects.filter(is_active=True, is_staff=True).order_by("email"),
        label=_("Assign to"),
        required=False,
        empty_label=_("Unassigned"),
    )


class SupportStatusForm(StyledForm):
    """Move a ticket along the support graph; the desk's service decides if it is legal."""

    status = forms.ChoiceField(label=_("Status"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = SupportTicket.Status.choices


class StaffRoleForm(StyledForm):
    """Grant and revoke back-office groups; only groups this app owns are offered."""

    add = forms.MultipleChoiceField(
        label=_("Grant"), choices=[], required=False, widget=forms.CheckboxSelectMultiple
    )
    remove = forms.MultipleChoiceField(
        label=_("Revoke"),
        choices=[],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(name, name) for name in BACKOFFICE_GROUPS]
        self.fields["add"].choices = choices
        self.fields["remove"].choices = choices
