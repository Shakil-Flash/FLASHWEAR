"""Celery background tasks for Business Automation (Phase 56)."""

from __future__ import annotations

import logging

from celery import shared_task

from apps.automation.services import evaluate_all_active_rules, evaluate_rules_for_trigger

logger = logging.getLogger("flashwear.automation")

__all__ = ["evaluate_scheduled_automations"]


@shared_task(name="automation.evaluate_rules", ignore_result=True)
def evaluate_scheduled_automations(trigger_type: str | None = None) -> int:
    """Evaluate active automation rules in the background.

    Idempotent by construction: individual rules enforce locking and cooldown windows.
    Returns the count of rules evaluated.
    """
    logger.info("Starting background automation evaluation (trigger=%s)", trigger_type)
    if trigger_type:
        logs = evaluate_rules_for_trigger(trigger_type)
    else:
        logs = evaluate_all_active_rules()
    logger.info("Completed background automation evaluation: %d rules processed", len(logs))
    return len(logs)
