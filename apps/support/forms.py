"""Forms for the FLASH Support HTML surfaces (Phase 15).

Declared fields are the *entire* contract between the browser and the service. Anything a
customer must not decide -- priority, status, assignment, ``is_internal`` -- has no field
here, so a crafted POST can carry it all it likes and nothing will read it. Reference
fields are validated by :mod:`apps.support.services.references`, which is where ownership
actually lives; the forms only gather the handles.

Widget styling comes from :func:`apps.accounts.forms.input_attrs` rather than from the
templates, so the account area keeps exactly one definition of what an input looks like.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.forms import input_attrs
from apps.support.models import SupportTicket
from apps.support.services.references import CUSTOMER_REFERENCE_FIELDS

__all__ = [
    "MessageForm",
    "StaffAssignForm",
    "StaffEscalateForm",
    "StaffLinkForm",
    "StaffNoteForm",
    "StaffPriorityForm",
    "StaffStatusForm",
    "TicketCreateForm",
]


class TicketCreateForm(forms.Form):
    """Opening a ticket: what happened, and (optionally) what it is about."""

    subject = forms.CharField(
        label=_("Subject"),
        max_length=200,
        widget=forms.TextInput(attrs=input_attrs(placeholder="Short summary", maxlength=200)),
        help_text=_("One line that says what is wrong."),
    )
    category = forms.ChoiceField(
        label=_("What is this about?"),
        choices=SupportTicket.Category.choices,
        initial=SupportTicket.Category.OTHER,
        widget=forms.Select(attrs=input_attrs()),
    )
    description = forms.CharField(
        label=_("Message"),
        widget=forms.Textarea(attrs=input_attrs(rows=6, maxlength=10000)),
        help_text=_("Plain text. Tell us what happened and what you expected."),
    )
    # References are optional handles. ``priority`` is deliberately absent: urgency is the
    # desk's judgement, so there is no field for a requester to set it.
    order = forms.CharField(
        label=_("Order number"),
        required=False,
        widget=forms.TextInput(attrs=input_attrs(placeholder="FW-2026-000001")),
        help_text=_("e.g. FW-2026-000001"),
    )
    product = forms.CharField(
        label=_("Product"),
        required=False,
        widget=forms.TextInput(attrs=input_attrs(placeholder="relaxed-tee")),
        help_text=_("The product's web address, e.g. relaxed-tee"),
    )
    review = forms.IntegerField(
        label=_("Review"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs=input_attrs()),
        help_text=_("Your review's number."),
    )
    loop_item = forms.IntegerField(
        label=_("FLASH Loop item"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs=input_attrs()),
        help_text=_("The item's number in your FLASH Loop dashboard."),
    )
    quest = forms.CharField(
        label=_("Quest"),
        required=False,
        widget=forms.TextInput(attrs=input_attrs(placeholder="first-drop")),
        help_text=_("The quest's web address."),
    )

    def references(self) -> dict:
        """Only the handles that were actually filled in, keyed for the resolver."""
        cleaned = self.cleaned_data
        return {
            field: cleaned.get(field)
            for field in CUSTOMER_REFERENCE_FIELDS
            if cleaned.get(field) not in (None, "")
        }


class MessageForm(forms.Form):
    """One reply. Files are collected separately with ``request.FILES.getlist``."""

    body = forms.CharField(
        label=_("Message"),
        widget=forms.Textarea(attrs=input_attrs(rows=4, maxlength=10000)),
    )


class StaffAssignForm(forms.Form):
    """Claim, reassign or (blank) release a ticket.

    The desk is a choice list, not a free-text email: assigning to an address that is not
    on the desk would silently park a ticket with somebody who can never see it.
    """

    agent_email = forms.ChoiceField(
        label=_("Assign to"),
        required=False,
        choices=(),
        widget=forms.Select(attrs=input_attrs()),
        help_text=_("Leave blank to return the ticket to the queue."),
    )

    def __init__(self, *args, agents=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "—")]
        choices += [(agent.email, agent.email) for agent in (agents or [])]
        self.fields["agent_email"].choices = choices


class StaffStatusForm(forms.Form):
    """Move a ticket along the graph."""

    status = forms.ChoiceField(
        label=_("Status"),
        choices=SupportTicket.Status.choices,
        widget=forms.Select(attrs=input_attrs()),
    )


class StaffPriorityForm(forms.Form):
    """Set urgency. There is no customer-facing equivalent of this form."""

    priority = forms.ChoiceField(
        label=_("Priority"),
        choices=SupportTicket.Priority.choices,
        widget=forms.Select(attrs=input_attrs()),
    )


class StaffEscalateForm(forms.Form):
    """Escalate with a reason. The reason is mandatory: an escalation nobody can read is a
    status change with extra steps."""

    reason = forms.CharField(
        label=_("Why is this escalated?"),
        widget=forms.Textarea(attrs=input_attrs(rows=3, maxlength=2000)),
    )
    target = forms.ChoiceField(
        label=_("Escalate to"),
        required=False,
        choices=(
            ("", "—"),
            ("tier_2", "Tier 2"),
            ("team_lead", "Team lead"),
            ("on_call", "On call"),
        ),
        widget=forms.Select(attrs=input_attrs()),
    )


class StaffLinkForm(forms.Form):
    """Attach an existing record to a ticket. Primary keys only -- this is an operator
    tool, and an operator knows (or can read) an id."""

    order = forms.IntegerField(
        label=_("Order"), required=False, min_value=1, widget=forms.NumberInput(attrs=input_attrs())
    )
    payment = forms.IntegerField(
        label=_("Payment"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs=input_attrs()),
    )
    shipment = forms.IntegerField(
        label=_("Shipment"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs=input_attrs()),
    )
    product = forms.IntegerField(
        label=_("Product"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs=input_attrs()),
    )
    review = forms.IntegerField(
        label=_("Review"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs=input_attrs()),
    )
    loop_item = forms.IntegerField(
        label=_("FLASH Loop item"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs=input_attrs()),
    )
    quest = forms.IntegerField(
        label=_("Quest"), required=False, min_value=1, widget=forms.NumberInput(attrs=input_attrs())
    )

    def references(self) -> dict:
        """Only the fields the operator filled in: a blank box means "leave it alone",
        never "unlink it" -- silently dropping a reference because a form was left alone
        would lose the context the ticket exists for. Clearing is an explicit API call."""
        cleaned = self.cleaned_data
        return {field: value for field, value in cleaned.items() if value not in (None, "")}


class StaffNoteForm(MessageForm):
    """Internal note. Same shape as a reply; the view decides which service it calls."""

    body = forms.CharField(
        label=_("Internal note"),
        widget=forms.Textarea(attrs=input_attrs(rows=3, maxlength=10000)),
        help_text=_("Only the support team can see this."),
    )
