"""Server-side validation for automation rules and configurations (Phase 56).

Ensures all rules adhere to strict allowlists, preventing arbitrary code execution,
malformed parameters, out-of-range thresholds, or unauthorized capabilities.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.automation.constants import (
    ACTION_CONFIG_SCHEMAS,
    ALLOWED_STAFF_CAPABILITIES,
    TRIGGER_ACTION_COMPATIBILITY,
    TRIGGER_CONDITION_SCHEMAS,
    ActionType,
    TriggerType,
)


def validate_rule_config(
    *,
    trigger_type: str,
    action_type: str,
    conditions: dict[str, Any] | None,
    action_config: dict[str, Any] | None,
) -> None:
    """Validate full rule configuration. Raises ValidationError on any breach."""
    if trigger_type not in TriggerType.values:
        raise ValidationError(
            _("Unsupported trigger type: %(trigger)s") % {"trigger": trigger_type}
        )

    if action_type not in ActionType.values:
        raise ValidationError(_("Unsupported action type: %(action)s") % {"action": action_type})

    allowed_actions = TRIGGER_ACTION_COMPATIBILITY.get(trigger_type, ())
    if action_type not in allowed_actions:
        raise ValidationError(
            _("Action '%(action)s' is not compatible with trigger '%(trigger)s'.")
            % {"action": action_type, "trigger": trigger_type}
        )

    validate_conditions(trigger_type, conditions or {})
    validate_action_config(action_type, action_config or {})


def validate_conditions(trigger_type: str, conditions: dict[str, Any]) -> None:
    """Validate trigger conditions dictionary against the schema."""
    if not isinstance(conditions, dict):
        raise ValidationError(_("Conditions must be a valid JSON dictionary."))

    allowed_keys = TRIGGER_CONDITION_SCHEMAS.get(trigger_type, {})
    for key, val in conditions.items():
        if key not in allowed_keys:
            raise ValidationError(
                _("Unrecognized condition '%(key)s' for trigger '%(trigger)s'.")
                % {"key": key, "trigger": trigger_type}
            )

        expected_type = allowed_keys[key]
        if not isinstance(val, expected_type):
            raise ValidationError(
                _("Condition '%(key)s' must be an integer, got %(type)s.")
                % {"key": key, "type": type(val).__name__}
            )

        # Range bounds
        if key in ("threshold", "days_overdue", "sla_hours", "min_attempts"):
            if val < 0:
                raise ValidationError(
                    _("Condition '%(key)s' must be a non-negative integer.") % {"key": key}
                )
            if val > 10000:
                raise ValidationError(
                    _("Condition '%(key)s' exceeds maximum allowable limit.") % {"key": key}
                )


def validate_action_config(action_type: str, config: dict[str, Any]) -> None:
    """Validate action configuration against schema."""
    if not isinstance(config, dict):
        raise ValidationError(_("Action configuration must be a valid JSON dictionary."))

    allowed_keys = ACTION_CONFIG_SCHEMAS.get(action_type, {})
    for key in config:
        if key not in allowed_keys:
            raise ValidationError(
                _("Unrecognized config key '%(key)s' for action '%(action)s'.")
                % {"key": key, "action": action_type}
            )

    # Specific action validations
    if action_type == ActionType.CREATE_STAFF_ALERT:
        capability = config.get("staff_capability", "")
        if capability and capability not in ALLOWED_STAFF_CAPABILITIES:
            raise ValidationError(
                _("Invalid staff capability '%(cap)s' for alert action.") % {"cap": capability}
            )

    elif action_type == ActionType.RECORD_AUDIT_LOG:
        domain = config.get("domain", "")
        from apps.backoffice.models import AuditEvent

        if domain and domain not in AuditEvent.Domain.values:
            raise ValidationError(_("Invalid audit domain '%(domain)s'.") % {"domain": domain})
