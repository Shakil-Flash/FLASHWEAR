"""Forms for staff portal authentication and account management."""

from __future__ import annotations

from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.staff.constants import StaffRole
from apps.staff.permissions import is_owner_or_superuser

User = get_user_model()


LG_INPUT_CLASSES = (
    "block w-full rounded-lg border border-slate-300 dark:border-slate-700 "
    "bg-white dark:bg-slate-900 px-3.5 py-2.5 text-sm text-slate-900 dark:text-white "
    "placeholder-slate-400 focus:border-amber-500 focus:outline-none "
    "focus:ring-2 focus:ring-amber-500/20"
)
MD_INPUT_CLASSES = (
    "w-full rounded-lg border border-slate-300 dark:border-slate-700 "
    "bg-white dark:bg-slate-900 px-3.5 py-2 text-sm text-slate-900 dark:text-white "
    "focus:border-amber-500 focus:ring-1 focus:ring-amber-500"
)
CHECKBOX_CLASSES = (
    "h-4 w-4 rounded border-slate-300 dark:border-slate-700 text-amber-500 focus:ring-amber-500"
)


class StaffLoginForm(forms.Form):
    """Clean credentials form for staff portal sign-in."""

    email = forms.EmailField(
        label=_("Work Email"),
        widget=forms.EmailInput(
            attrs={
                "class": LG_INPUT_CLASSES,
                "placeholder": "name@flashwear.com",
                "autocomplete": "email",
                "required": True,
            }
        ),
    )
    password = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput(
            attrs={
                "class": LG_INPUT_CLASSES,
                "placeholder": "••••••••••••",
                "autocomplete": "current-password",
                "required": True,
            }
        ),
    )
    remember_me = forms.BooleanField(
        label=_("Stay signed in on this device"),
        required=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECKBOX_CLASSES,
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        email = User.normalize_email_value(cleaned_data.get("email") or "")
        password = cleaned_data.get("password")

        if email and password:
            user = authenticate(username=email, password=password)
            if user is None:
                raise ValidationError(_("Invalid email address or password."))
            if not user.is_active:
                raise ValidationError(_("This account is inactive. Please contact your manager."))
            cleaned_data["user"] = user
        return cleaned_data


class StaffAccountCreateForm(forms.Form):
    """Form to create or invite a staff member."""

    email = forms.EmailField(
        label=_("Work Email"),
        widget=forms.EmailInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "employee@flashwear.com",
            }
        ),
    )
    first_name = forms.CharField(
        label=_("First Name"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
            }
        ),
    )
    last_name = forms.CharField(
        label=_("Last Name"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
            }
        ),
    )
    roles = forms.MultipleChoiceField(
        label=_("Assigned Roles"),
        choices=StaffRole.choices,
        widget=forms.CheckboxSelectMultiple(
            attrs={
                "class": CHECKBOX_CLASSES,
            }
        ),
        help_text=_("Select one or more operational roles for this employee."),
    )
    password = forms.CharField(
        label=_("Initial Password (optional)"),
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "Leave blank to auto-generate a secure password",
            }
        ),
        help_text=_("If left blank, a secure 16-character temporary password will be generated."),
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        # If actor is not owner/superuser, filter out OWNER to prevent privilege escalation
        if self.actor and not is_owner_or_superuser(self.actor):
            self.fields["roles"].choices = [
                (val, lbl) for val, lbl in StaffRole.choices if val != StaffRole.OWNER
            ]

    def clean_email(self):
        email = User.normalize_email_value(self.cleaned_data.get("email") or "")
        if User.objects.filter(email=email).exists():
            raise ValidationError(_("A user with this email address already exists."))
        return email

    def clean_roles(self):
        roles = self.cleaned_data.get("roles") or []
        if not roles:
            raise ValidationError(_("Please select at least one role."))
        if StaffRole.OWNER in roles and self.actor and not is_owner_or_superuser(self.actor):
            raise ValidationError(_("Only an Owner can assign the Owner role."))
        return roles


class StaffAccountRoleForm(forms.Form):
    """Form to modify roles assigned to a staff member."""

    roles = forms.MultipleChoiceField(
        label=_("Assigned Roles"),
        choices=StaffRole.choices,
        widget=forms.CheckboxSelectMultiple(
            attrs={
                "class": CHECKBOX_CLASSES,
            }
        ),
    )

    def __init__(self, *args, actor=None, target_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.target_user = target_user
        if self.actor and not is_owner_or_superuser(self.actor):
            self.fields["roles"].choices = [
                (val, lbl) for val, lbl in StaffRole.choices if val != StaffRole.OWNER
            ]

    def clean_roles(self):
        roles = self.cleaned_data.get("roles") or []
        if not roles:
            raise ValidationError(_("A staff member must have at least one role."))
        if StaffRole.OWNER in roles and self.actor and not is_owner_or_superuser(self.actor):
            raise ValidationError(_("Only an Owner can assign the Owner role."))
        return roles


class OrderPackForm(forms.Form):
    """Warehouse packing form to record package specifications and internal notes."""

    package_weight_kg = forms.DecimalField(
        label=_("Package Weight (kg)"),
        required=False,
        min_value=0,
        max_digits=6,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. 0.85",
                "step": "0.01",
            }
        ),
    )
    package_dimensions = forms.CharField(
        label=_("Package Dimensions"),
        required=False,
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. 35 x 25 x 10 cm",
            }
        ),
    )
    internal_notes = forms.CharField(
        label=_("Packing Notes / Staff Checklist"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": MD_INPUT_CLASSES,
                "rows": 2,
                "placeholder": "Quality checked, branded tissue wrapped...",
            }
        ),
    )


class OrderDispatchForm(forms.Form):
    """Courier handoff and dispatch form with carrier details and tracking identifiers."""

    carrier = forms.CharField(
        label=_("Courier / Carrier"),
        required=False,
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. DHL Express, FedEx, Royal Mail, UPS",
            }
        ),
    )
    carrier_service = forms.CharField(
        label=_("Carrier Service Level"),
        required=False,
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. Next Day Tracked, International Standard",
            }
        ),
    )
    tracking_number = forms.CharField(
        label=_("Tracking Number / Consignment #"),
        required=False,
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. GB123456789HR",
            }
        ),
    )
    tracking_url = forms.URLField(
        assume_scheme="https",
        label=_("External Tracking URL (Optional)"),
        required=False,
        widget=forms.URLInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "https://carrier.com/track/...",
            }
        ),
    )
    internal_notes = forms.CharField(
        label=_("Dispatch Notes"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": MD_INPUT_CLASSES,
                "rows": 2,
                "placeholder": "Manifested with evening carrier collection...",
            }
        ),
    )


class ShipmentAssignmentForm(forms.Form):
    """Assign or reassign an active shipment to an eligible delivery worker."""

    assigned_to = forms.ModelChoiceField(
        label=_("Delivery Staff Member"),
        queryset=User.objects.none(),
        empty_label=_("-- Select Delivery Worker --"),
        required=True,
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES}),
    )
    assignment_note = forms.CharField(
        label=_("Assignment Note (Optional)"),
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. Route 4, priority morning delivery...",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.staff.services import get_eligible_delivery_staff

        self.fields["assigned_to"].queryset = get_eligible_delivery_staff()


class ShipmentStatusUpdateForm(forms.Form):
    """Operational checkpoint or transit status update form."""

    target_status = forms.ChoiceField(
        label=_("Update Status To"),
        choices=[],
        required=True,
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES}),
    )
    location = forms.CharField(
        label=_("Current Location / Checkpoint"),
        required=False,
        max_length=128,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. Central Logistics Hub, Delivery Van 4B",
            }
        ),
    )
    note = forms.CharField(
        label=_("Status Note"),
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. Departed distribution facility, in transit...",
            }
        ),
    )

    def __init__(self, *args, shipment=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.orders.models import Shipment

        if shipment:
            allowed = Shipment.ALLOWED_TRANSITIONS.get(shipment.status, ())
            choices = []
            for status_val in allowed:
                # Exclude internal picking/packed transitions from delivery transit flow
                if status_val not in (Shipment.Status.CANCELLED,):
                    choices.append(
                        (
                            status_val,
                            dict(Shipment.Status.choices).get(status_val, status_val),
                        )
                    )
            self.fields["target_status"].choices = choices


class DeliveryAttemptForm(forms.Form):
    """Record a failed delivery attempt or exception with standardized reasons."""

    REASON_CHOICES = [
        ("recipient_unavailable", _("Recipient Unavailable / Not at Home")),
        ("address_incomplete", _("Address Incomplete or Incorrect")),
        ("recipient_declined", _("Recipient Declined Delivery")),
        (
            "operational_issue",
            _("Operational Issue / Access Blocked / Severe Weather"),
        ),
    ]

    failure_reason = forms.ChoiceField(
        label=_("Failure Reason"),
        choices=REASON_CHOICES,
        required=True,
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES}),
    )
    location = forms.CharField(
        label=_("Attempt Location"),
        required=False,
        max_length=128,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. Customer Doorstep, Building Intercom",
            }
        ),
    )
    next_attempt_date = forms.DateField(
        label=_("Next Scheduled Attempt"),
        required=False,
        widget=forms.DateInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "type": "date",
            }
        ),
    )
    requires_manager_review = forms.BooleanField(
        label=_("Flag for Manager Review"),
        required=False,
        widget=forms.CheckboxInput(attrs={"class": CHECKBOX_CLASSES}),
    )
    note = forms.CharField(
        label=_("Driver Operational Notes"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": MD_INPUT_CLASSES,
                "rows": 2,
                "placeholder": "Left calling card #1042 behind mailbox; neighbor unavailable...",
            }
        ),
    )


class ProofOfDeliveryForm(forms.Form):
    """Secure proof of delivery verification via customer OTP or recipient sign-off."""

    POD_TYPE_CHOICES = [
        ("otp", _("Customer Confirmation Code (OTP)")),
        ("recipient_signoff", _("Recipient Name & Direct Handover Sign-off")),
    ]

    pod_type = forms.ChoiceField(
        label=_("Verification Method"),
        choices=POD_TYPE_CHOICES,
        required=True,
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES, "x-model": "podType"}),
    )
    otp_code = forms.CharField(
        label=_("6-Digit OTP Code"),
        required=False,
        max_length=12,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "123456",
                "maxlength": "6",
                "pattern": "[0-9]{6}",
            }
        ),
    )
    recipient_name = forms.CharField(
        label=_("Recipient Full Name / Relationship"),
        required=False,
        max_length=128,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. Jane Doe (Resident / Concierge)",
            }
        ),
    )
    notes = forms.CharField(
        label=_("Delivery Handover Notes"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": MD_INPUT_CLASSES,
                "rows": 2,
                "placeholder": "Package handed over in good condition...",
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        pod_type = cleaned_data.get("pod_type")
        otp_code = (cleaned_data.get("otp_code") or "").strip()
        recipient_name = (cleaned_data.get("recipient_name") or "").strip()

        if pod_type == "otp":
            if not otp_code:
                self.add_error("otp_code", _("The 6-digit confirmation code is required."))
            elif not otp_code.isdigit() or len(otp_code) != 6:
                self.add_error("otp_code", _("The confirmation code must be exactly 6 digits."))
        elif pod_type == "recipient_signoff":
            if not recipient_name:
                self.add_error(
                    "recipient_name",
                    _("Recipient name is required for manual sign-off."),
                )
            elif len(recipient_name) < 2:
                self.add_error(
                    "recipient_name",
                    _("Recipient name must be at least 2 characters."),
                )

        return cleaned_data


# =============================================================================
# Phase 62: Inventory & Stock Operations Forms
# =============================================================================


class StockAdjustmentForm(forms.Form):
    """Form to safely record stock adjustments (discrepancy, damage, loss)."""

    ADJUSTMENT_TYPE_CHOICES = [
        ("correction_add", _("Stock Discrepancy (Add units)")),
        ("correction_sub", _("Stock Discrepancy (Deduct units)")),
        ("damaged", _("Damaged / Unsellable Stock (Deduct)")),
        ("loss", _("Stock Loss / Shrinkage (Deduct)")),
    ]

    adjustment_type = forms.ChoiceField(
        label=_("Adjustment Type"),
        choices=ADJUSTMENT_TYPE_CHOICES,
        required=True,
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES}),
    )
    quantity = forms.IntegerField(
        label=_("Quantity"),
        min_value=1,
        required=True,
        widget=forms.NumberInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "min": "1",
                "placeholder": "e.g. 5",
            }
        ),
    )
    reason = forms.CharField(
        label=_("Reason for Adjustment"),
        required=True,
        min_length=3,
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. Cycle count variance, water damage in aisle B",
            }
        ),
    )
    reference = forms.CharField(
        label=_("Reference / Document #"),
        required=False,
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. AUDIT-2026-Q4",
            }
        ),
    )
    note = forms.CharField(
        label=_("Internal Notes"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": MD_INPUT_CLASSES,
                "rows": 2,
                "placeholder": "Additional operational context...",
            }
        ),
    )
    idempotency_token = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )


class StockReceiveForm(forms.Form):
    """Form to receive new stock shipments into on-hand inventory."""

    quantity = forms.IntegerField(
        label=_("Received Quantity"),
        min_value=1,
        required=True,
        widget=forms.NumberInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "min": "1",
                "placeholder": "e.g. 50",
            }
        ),
    )
    supplier_reference = forms.CharField(
        label=_("Supplier / Shipment Reference"),
        required=False,
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. PO-88321 or BOL-994",
            }
        ),
    )
    reason = forms.CharField(
        label=_("Receipt Description"),
        required=False,
        max_length=255,
        initial="Stock receipt from supplier",
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "Stock receipt from supplier",
            }
        ),
    )
    note = forms.CharField(
        label=_("Receiving Notes"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": MD_INPUT_CLASSES,
                "rows": 2,
                "placeholder": "Inspected batch #, pallet notes...",
            }
        ),
    )
    idempotency_token = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )


class StockThresholdForm(forms.Form):
    """Form to update variant-specific low stock reorder threshold."""

    low_stock_threshold = forms.IntegerField(
        label=_("Reorder Threshold"),
        required=False,
        min_value=0,
        max_value=9999,
        help_text=_("Leave blank to inherit product-level or global threshold."),
        widget=forms.NumberInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "min": "0",
                "max": "9999",
                "placeholder": "e.g. 10 (or blank to inherit)",
            }
        ),
    )


class StockReversalForm(forms.Form):
    """Form for authorized managers to record a compensating reversal of a previous movement."""

    movement_id = forms.IntegerField(
        widget=forms.HiddenInput(),
        required=True,
    )
    reason = forms.CharField(
        label=_("Reason for Reversal"),
        required=True,
        min_length=3,
        max_length=255,
        widget=forms.Textarea(
            attrs={
                "class": MD_INPUT_CLASSES,
                "rows": 2,
                "placeholder": "e.g. Reversing duplicate receipt from earlier today...",
            }
        ),
    )


# --------------------------------------------------------------------------------------
# Staff Task & Collaboration Forms (Phase 63)
# --------------------------------------------------------------------------------------


class StaffTaskCreateForm(forms.Form):
    """Form to create a new staff task."""

    title = forms.CharField(
        label=_("Task Title"),
        max_length=255,
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. Investigate delivery exception for order #10024",
            }
        ),
    )
    description = forms.CharField(
        label=_("Description"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": MD_INPUT_CLASSES,
                "rows": 4,
                "placeholder": "Operational details, instructions, or investigation context...",
            }
        ),
    )
    priority = forms.ChoiceField(
        label=_("Priority"),
        required=True,
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES}),
    )
    assignee = forms.ModelChoiceField(
        label=_("Assignee"),
        queryset=User.objects.none(),
        required=False,
        empty_label=_("-- Unassigned (Open Pool) --"),
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES}),
    )
    due_date = forms.DateTimeField(
        label=_("Due Date & Time"),
        required=False,
        widget=forms.DateTimeInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "type": "datetime-local",
            }
        ),
    )
    related_object_type = forms.ChoiceField(
        label=_("Linked Business Object Type"),
        required=False,
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES}),
    )
    related_object_id = forms.IntegerField(
        label=_("Linked Object ID / Number"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "placeholder": "e.g. Order ID, Shipment ID, Variant ID",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.staff.models import StaffTaskPriority, StaffTaskRelatedType
        from apps.staff.task_services import get_eligible_assignees

        self.fields["priority"].choices = StaffTaskPriority.choices
        self.fields["priority"].initial = StaffTaskPriority.NORMAL
        self.fields["related_object_type"].choices = StaffTaskRelatedType.choices
        self.fields["assignee"].queryset = get_eligible_assignees()
        self.fields["assignee"].label_from_instance = lambda u: (
            f"{u.get_full_name()} ({u.email})" if u.get_full_name() else u.email
        )


class StaffTaskUpdateForm(forms.Form):
    """Form to edit general task fields."""

    title = forms.CharField(
        label=_("Task Title"),
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={"class": MD_INPUT_CLASSES}),
    )
    description = forms.CharField(
        label=_("Description"),
        required=False,
        widget=forms.Textarea(attrs={"class": MD_INPUT_CLASSES, "rows": 4}),
    )
    priority = forms.ChoiceField(
        label=_("Priority"),
        required=True,
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES}),
    )
    due_date = forms.DateTimeField(
        label=_("Due Date & Time"),
        required=False,
        widget=forms.DateTimeInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "type": "datetime-local",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.staff.models import StaffTaskPriority

        self.fields["priority"].choices = StaffTaskPriority.choices


class StaffTaskAssignForm(forms.Form):
    """Form to reassign or unassign a staff task."""

    assignee = forms.ModelChoiceField(
        label=_("Assign To"),
        queryset=User.objects.none(),
        required=False,
        empty_label=_("-- Unassign (Move to Open Pool) --"),
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.staff.task_services import get_eligible_assignees

        self.fields["assignee"].queryset = get_eligible_assignees()
        self.fields["assignee"].label_from_instance = lambda u: (
            f"{u.get_full_name()} ({u.email})" if u.get_full_name() else u.email
        )


class StaffTaskStatusForm(forms.Form):
    """Form to update task status."""

    status = forms.ChoiceField(
        label=_("Status"),
        required=True,
        widget=forms.Select(attrs={"class": MD_INPUT_CLASSES}),
    )

    def __init__(self, *args, allowed_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.staff.models import StaffTaskStatus

        if allowed_choices is not None:
            self.fields["status"].choices = allowed_choices
        else:
            self.fields["status"].choices = StaffTaskStatus.choices


class StaffTaskProgressForm(forms.Form):
    """Form to update task progress percentage."""

    progress_percent = forms.IntegerField(
        label=_("Progress Percentage"),
        required=True,
        min_value=0,
        max_value=100,
        widget=forms.NumberInput(
            attrs={
                "class": MD_INPUT_CLASSES,
                "min": "0",
                "max": "100",
                "step": "5",
            }
        ),
    )


class StaffTaskCommentForm(forms.Form):
    """Form to post an operational note or progress update to a task."""

    comment = forms.CharField(
        label=_("Add Operational Note / Comment"),
        required=True,
        widget=forms.Textarea(
            attrs={
                "class": MD_INPUT_CLASSES,
                "rows": 3,
                "placeholder": (
                    "Document actions taken, findings, blockers, or handoff notes..."
                ),
            }
        ),
    )
