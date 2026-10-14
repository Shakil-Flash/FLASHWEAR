"""Forms for Automation Rule creation and management (Phase 56)."""

from __future__ import annotations

from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.automation.constants import (
    ALLOWED_STAFF_CAPABILITIES,
    ActionType,
    TriggerType,
)
from apps.automation.models import AutomationRule
from apps.automation.validators import validate_rule_config


class AutomationRuleForm(forms.ModelForm):
    """Staff form for creating and updating business automation rules."""

    # Explicit condition helper fields
    threshold = forms.IntegerField(
        label=_("Stock Threshold"),
        required=False,
        min_value=0,
        initial=5,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "5"}),
        help_text=_("Units on-hand at or below which this alert fires."),
    )
    days_overdue = forms.IntegerField(
        label=_("Days Overdue"),
        required=False,
        min_value=0,
        initial=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "0"}),
        help_text=_("Grace days past invoice due date before flagging (0 = on due date)."),
    )
    sla_hours = forms.IntegerField(
        label=_("SLA Waiting Threshold (Hours)"),
        required=False,
        min_value=1,
        initial=24,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "24"}),
        help_text=_("Hours an order or support ticket may wait before triggering."),
    )

    # Explicit action helper fields
    staff_capability = forms.ChoiceField(
        label=_("Target Staff Capability"),
        required=False,
        choices=[("", _("All Authorized Operations Staff"))]
        + [(c, c) for c in ALLOWED_STAFF_CAPABILITIES],
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text=_("Only notify staff holding this backoffice capability."),
    )
    reason = forms.CharField(
        label=_("Action Reason / Note"),
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "e.g. Automated SLA escalation"}
        ),
        help_text=_("Audit trail explanation recorded when the action fires."),
    )
    target = forms.CharField(
        label=_("Escalation Target Tier"),
        required=False,
        max_length=20,
        initial="tier_2",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "tier_2"}),
        help_text=_("Target queue or escalation team code (e.g. tier_2, fraud, manager)."),
    )

    class Meta:
        model = AutomationRule
        fields = [
            "name",
            "description",
            "trigger_type",
            "action_type",
            "is_active",
            "cooldown_minutes",
            "max_retries",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": _("e.g. Critical Low Stock Alert")}
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 2,
                    "placeholder": _("Rule purpose and behavior..."),
                }
            ),
            "trigger_type": forms.Select(attrs={"class": "form-select"}),
            "action_type": forms.Select(attrs={"class": "form-select"}),
            "cooldown_minutes": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "max_retries": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            conds = self.instance.conditions or {}
            cfg = self.instance.action_config or {}
            if "threshold" in conds:
                self.fields["threshold"].initial = conds["threshold"]
            if "days_overdue" in conds:
                self.fields["days_overdue"].initial = conds["days_overdue"]
            if "sla_hours" in conds:
                self.fields["sla_hours"].initial = conds["sla_hours"]

            if "staff_capability" in cfg:
                self.fields["staff_capability"].initial = cfg["staff_capability"]
            if "reason" in cfg:
                self.fields["reason"].initial = cfg["reason"]
            elif "note" in cfg:
                self.fields["reason"].initial = cfg["note"]
            if "target" in cfg:
                self.fields["target"].initial = cfg["target"]

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean()
        trigger_type = cleaned_data.get("trigger_type")
        action_type = cleaned_data.get("action_type")

        # Construct conditions dictionary from helper fields
        conditions: dict[str, Any] = {}
        if trigger_type in (TriggerType.SCHEDULED_LOW_STOCK, TriggerType.EVENT_LOW_STOCK):
            val = cleaned_data.get("threshold")
            if val is not None:
                conditions["threshold"] = val
        elif trigger_type == TriggerType.SCHEDULED_OVERDUE_INVOICES:
            val = cleaned_data.get("days_overdue")
            if val is not None:
                conditions["days_overdue"] = val
        elif trigger_type in (
            TriggerType.SCHEDULED_PENDING_FULFILLMENT,
            TriggerType.SCHEDULED_SUPPORT_TICKET_SLA,
        ):
            val = cleaned_data.get("sla_hours")
            if val is not None:
                conditions["sla_hours"] = val
        elif trigger_type in (
            TriggerType.SCHEDULED_SHIPMENT_EXCEPTIONS,
            TriggerType.EVENT_DELIVERY_EXCEPTION,
        ):
            conditions["min_attempts"] = 1

        # Construct action_config dictionary from helper fields
        action_config: dict[str, Any] = {}
        if action_type == ActionType.CREATE_STAFF_ALERT:
            cap = cleaned_data.get("staff_capability")
            if cap:
                action_config["staff_capability"] = cap
            action_config["headline_template"] = str(_("Automation Alert: {target}"))
            action_config["message_template"] = str(_("Threshold breach detected for: {target}"))
        elif action_type == ActionType.ESCALATE_SUPPORT_TICKET:
            action_config["reason"] = cleaned_data.get("reason") or str(
                _("Automated ticket SLA breach.")
            )
            action_config["target"] = cleaned_data.get("target") or "tier_2"
        elif action_type == ActionType.FLAG_SUPPLIER_INVOICE:
            action_config["note"] = cleaned_data.get("reason") or str(
                _("Automated overdue invoice flag.")
            )
        elif action_type == ActionType.RECORD_AUDIT_LOG:
            action_config["domain"] = "staff"
            action_config["action"] = "automation_executed"
            action_config["reason"] = cleaned_data.get("reason") or str(
                _("Automated operational audit.")
            )

        # Server-side validation
        try:
            validate_rule_config(
                trigger_type=trigger_type,
                action_type=action_type,
                conditions=conditions,
                action_config=action_config,
            )
        except ValidationError as exc:
            raise ValidationError(exc.messages) from exc

        self.instance.conditions = conditions
        self.instance.action_config = action_config
        return cleaned_data
