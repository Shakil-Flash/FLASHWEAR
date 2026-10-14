"""Management command to evaluate business automation rules (Phase 56).

Usage:
    python manage.py run_automations
    python manage.py run_automations --trigger scheduled_low_stock
    python manage.py run_automations --rule-id 1 --force
    python manage.py run_automations --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.automation.constants import TriggerType
from apps.automation.engine import AutomationEngine
from apps.automation.models import AutomationRule
from apps.automation.services import (
    evaluate_rule,
)


class Command(BaseCommand):
    help = "Safely evaluate and execute configured business automation rules."

    def add_arguments(self, parser):
        parser.add_argument(
            "--trigger",
            type=str,
            choices=TriggerType.values,
            help="Filter execution to a specific trigger type.",
        )
        parser.add_argument(
            "--rule-id",
            type=int,
            help="Evaluate a specific automation rule by ID.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Bypass cooldown windows and concurrent run locks.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Inspect matching candidates without executing actions.",
        )

    def handle(self, *args, **options):
        trigger = options.get("trigger")
        rule_id = options.get("rule_id")
        force = options.get("force", False)
        dry_run = options.get("dry_run", False)

        self.stdout.write(
            self.style.NOTICE(
                f"[{timezone.now():%Y-%m-%d %H:%M:%S}] Starting automation evaluation "
                f"(dry_run={dry_run}, force={force})..."
            )
        )

        if rule_id:
            try:
                rule = AutomationRule.objects.get(pk=rule_id)
            except AutomationRule.DoesNotExist:
                raise CommandError(f"AutomationRule with id {rule_id} does not exist.") from None

            rules = [rule]
        elif trigger:
            rules = list(AutomationRule.objects.filter(trigger_type=trigger, is_active=True))
        else:
            rules = list(AutomationRule.objects.filter(is_active=True))

        if not rules:
            self.stdout.write(self.style.WARNING("No matching active automation rules found."))
            return

        for rule in rules:
            self.stdout.write(
                f"- Evaluating rule: '{rule.name}' "
                f"(Trigger: {rule.trigger_type}, Action: {rule.action_type})"
            )
            if dry_run:
                engine = AutomationEngine(rule, force=True)
                candidates = engine._find_candidates()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [DRY-RUN] Found {len(candidates)} matching candidate(s):"
                    )
                )
                for _, target_id, target_repr in candidates[:5]:
                    self.stdout.write(f"    * {target_id}: {target_repr}")
                if len(candidates) > 5:
                    self.stdout.write(f"    ... and {len(candidates) - 5} more.")
            else:
                log = evaluate_rule(rule, force=force)
                color = self.style.SUCCESS if log.status == "success" else self.style.WARNING
                summary = (
                    f"  Status: {log.status.upper()} | Evaluated: {log.items_evaluated} "
                    f"| Affected: {log.items_affected} | Duration: {log.duration_ms}ms"
                )
                self.stdout.write(color(summary))
                if log.error_summary:
                    self.stdout.write(self.style.ERROR(f"  Error: {log.error_summary}"))

        self.stdout.write(self.style.NOTICE("Automation evaluation completed."))
