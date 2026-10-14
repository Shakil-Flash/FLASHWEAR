"""Service API for Business Automation (Phase 56).

Safe public entry point for domain events, management commands, and staff views.
"""

from __future__ import annotations

import logging
from typing import Any

from apps.automation.engine import AutomationEngine
from apps.automation.models import AutomationExecutionLog, AutomationRule
from apps.backoffice.models import AuditEvent

logger = logging.getLogger("flashwear.automation")

__all__ = [
    "evaluate_all_active_rules",
    "evaluate_event",
    "evaluate_rule",
    "evaluate_rules_for_trigger",
    "toggle_rule",
]


def evaluate_rule(
    rule: AutomationRule,
    *,
    actor=None,
    force: bool = False,
) -> AutomationExecutionLog:
    """Evaluate and execute a single automation rule."""
    if not rule.is_active and not force:
        return AutomationExecutionLog.objects.create(
            rule=rule,
            trigger_event=rule.trigger_type,
            status="skipped",
            target_repr=rule.name,
            items_evaluated=0,
            items_affected=0,
            details={"reason": "rule_is_inactive"},
        )

    engine = AutomationEngine(rule, actor=actor, force=force)
    return engine.run()


def evaluate_rules_for_trigger(
    trigger_type: str,
    *,
    actor=None,
    force: bool = False,
) -> list[AutomationExecutionLog]:
    """Evaluate all active rules configured for a specific trigger."""
    rules = AutomationRule.objects.filter(trigger_type=trigger_type, is_active=True)
    results = []
    for rule in rules:
        log = evaluate_rule(rule, actor=actor, force=force)
        results.append(log)
    return results


def evaluate_all_active_rules(
    *,
    actor=None,
    force: bool = False,
) -> list[AutomationExecutionLog]:
    """Run all active rules (used by cron, management command, or Celery task)."""
    rules = AutomationRule.objects.filter(is_active=True)
    results = []
    for rule in rules:
        log = evaluate_rule(rule, actor=actor, force=force)
        results.append(log)
    return results


def evaluate_event(
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    actor=None,
) -> list[AutomationExecutionLog]:
    """Process an event-based trigger emitted by a domain service."""
    rules = AutomationRule.objects.filter(trigger_type=event_type, is_active=True)
    results = []
    for rule in rules:
        engine = AutomationEngine(rule, actor=actor, force=False)
        log = engine.run()
        results.append(log)
    return results


def toggle_rule(
    rule: AutomationRule,
    *,
    actor=None,
    is_active: bool | None = None,
) -> AutomationRule:
    """Enable or disable a rule with an append-only audit event."""
    new_state = (not rule.is_active) if is_active is None else is_active
    if rule.is_active == new_state:
        return rule

    old_state = rule.is_active
    rule.is_active = new_state
    rule.save(update_fields=["is_active", "updated_at"])

    action_label = "automation_rule_enabled" if new_state else "automation_rule_disabled"
    AuditEvent.objects.create(
        actor=actor,
        actor_label=getattr(actor, "email", "system"),
        domain=AuditEvent.Domain.STAFF,
        action=action_label,
        object_type="AutomationRule",
        object_id=str(rule.pk),
        object_repr=rule.name,
        reason=f"Rule {action_label} by {getattr(actor, 'email', 'system')}.",
        metadata={"rule_id": rule.pk, "was_active": old_state, "is_active": new_state},
    )

    return rule
