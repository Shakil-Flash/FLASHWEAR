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
