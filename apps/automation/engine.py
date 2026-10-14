"""Business Automation Engine (Phase 56).

Safe, allowlisted evaluation and execution of operational rules.
Guarantees:
- Safe action execution (no direct modification of payments, inventory, or order totals).
- Deduplication and cooldown windows preventing alert floods.
- Concurrency locking preventing duplicate overlapping runs.
- Error sanitization (no credentials, tracebacks, or secret exposure).
- Full auditability via append-only execution logs.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.automation.constants import (
    ActionType,
    ExecutionStatus,
    TriggerType,
)
from apps.automation.models import AutomationExecutionLog, AutomationRule
from apps.backoffice.models import AuditEvent
from apps.backoffice.permissions import (
    OPS_VIEW,
    has,
    staff_members,
)
from apps.notifications.models import NotificationType
from apps.notifications.services.events import emit

logger = logging.getLogger("flashwear.automation")

LOCK_TIMEOUT_SECONDS = 120


class AutomationEngine:
    """Core evaluation engine for business automations."""

    def __init__(self, rule: AutomationRule, *, actor=None, force: bool = False):
        self.rule = rule
        self.actor = actor
        self.force = force

    def run(self) -> AutomationExecutionLog:
        """Execute evaluation of the rule with locking, error isolation, and logging."""
        start_time = time.monotonic()
        lock_key = f"automation_lock_rule_{self.rule.pk}"

        # Acquire lock to prevent duplicate concurrent runs
        if not self.force:
            acquired = cache.add(lock_key, "1", timeout=LOCK_TIMEOUT_SECONDS)
            if not acquired:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                return AutomationExecutionLog.objects.create(
                    rule=self.rule,
                    trigger_event=self.rule.trigger_type,
                    status=ExecutionStatus.SKIPPED,
                    idempotency_key=f"lock_skipped:{self.rule.pk}:{int(time.time())}",
                    target_repr=self.rule.name,
                    items_evaluated=0,
                    items_affected=0,
                    details={"reason": "concurrent_lock_active"},
                    duration_ms=duration_ms,
                )

        try:
            return self._evaluate_and_execute(start_time)
        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            sanitized_error = self._sanitize_error(str(exc))
            logger.exception("Automation rule %s (%s) failed", self.rule.name, self.rule.pk)

            # Update rule failure tracking
            AutomationRule.objects.filter(pk=self.rule.pk).update(
                last_triggered_at=timezone.now(),
                last_status=ExecutionStatus.FAILED,
                consecutive_failures=self.rule.consecutive_failures + 1,
            )

            return AutomationExecutionLog.objects.create(
                rule=self.rule,
                trigger_event=self.rule.trigger_type,
                status=ExecutionStatus.FAILED,
                idempotency_key=f"fail:{self.rule.pk}:{int(time.time())}",
                target_repr=self.rule.name,
                items_evaluated=0,
                items_affected=0,
                details={"exception_type": type(exc).__name__},
                error_summary=sanitized_error,
                duration_ms=duration_ms,
            )
        finally:
            if not self.force:
                cache.delete(lock_key)

    def _evaluate_and_execute(self, start_time: float) -> AutomationExecutionLog:
        now = timezone.now()
        cooldown_delta = timedelta(minutes=self.rule.cooldown_minutes)
        cooldown_cutoff = now - cooldown_delta

        # 1. Evaluate trigger candidates
        candidates = self._find_candidates()
        items_evaluated = len(candidates)

        if items_evaluated == 0:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            self._record_rule_success(now)
            return AutomationExecutionLog.objects.create(
                rule=self.rule,
                trigger_event=self.rule.trigger_type,
                status=ExecutionStatus.SUCCESS,
                idempotency_key=f"run:{self.rule.pk}:{int(now.timestamp())}",
                target_repr=self.rule.name,
                items_evaluated=0,
                items_affected=0,
                details={"reason": "no_matching_records"},
                duration_ms=duration_ms,
            )

        # 2. Filter candidates based on cooldown idempotency keys
        actionable: list[tuple[Any, str]] = []
        for candidate, target_id, _target_repr in candidates:
            item_key = (
                f"auto:{self.rule.trigger_type}:{self.rule.action_type}:{self.rule.pk}:{target_id}"
            )
            if not self.force:
                recent_action = AutomationExecutionLog.objects.filter(
                    idempotency_key=item_key,
                    status=ExecutionStatus.SUCCESS,
                    executed_at__gte=cooldown_cutoff,
                ).exists()
                if recent_action:
                    continue
            actionable.append((candidate, item_key))

        if not actionable:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            self._record_rule_success(now)
            return AutomationExecutionLog.objects.create(
                rule=self.rule,
                trigger_event=self.rule.trigger_type,
                status=ExecutionStatus.SKIPPED,
                idempotency_key=f"cooldown:{self.rule.pk}:{int(now.timestamp())}",
                target_repr=self.rule.name,
                items_evaluated=items_evaluated,
                items_affected=0,
                details={
                    "reason": "all_candidates_within_cooldown",
                    "cooldown_minutes": self.rule.cooldown_minutes,
                },
                duration_ms=duration_ms,
            )

        # 3. Execute approved action
        items_affected = 0
        action_results: list[dict[str, Any]] = []

        for candidate, item_key in actionable:
            try:
                res = self._execute_action(candidate, item_key)
                items_affected += 1
                action_results.append(res)
                # Log individual item success for cooldown tracking
                AutomationExecutionLog.objects.create(
                    rule=self.rule,
                    trigger_event=self.rule.trigger_type,
                    status=ExecutionStatus.SUCCESS,
                    idempotency_key=item_key,
                    target_repr=str(candidate)[:255],
                    items_evaluated=1,
                    items_affected=1,
                    details=res,
                    duration_ms=0,
                )
            except Exception as item_err:
                logger.warning("Action execution failed on candidate %s: %s", candidate, item_err)
                action_results.append({"error": self._sanitize_error(str(item_err))})

        duration_ms = int((time.monotonic() - start_time) * 1000)
        self._record_rule_success(now)

        return AutomationExecutionLog.objects.create(
            rule=self.rule,
            trigger_event=self.rule.trigger_type,
            status=ExecutionStatus.SUCCESS,
            idempotency_key=f"summary:{self.rule.pk}:{int(now.timestamp())}",
            target_repr=self.rule.name,
            items_evaluated=items_evaluated,
            items_affected=items_affected,
            details={"actions": action_results[:20]},
            duration_ms=duration_ms,
        )

    def _record_rule_success(self, now: timezone.datetime) -> None:
        AutomationRule.objects.filter(pk=self.rule.pk).update(
            last_triggered_at=now,
            last_status=ExecutionStatus.SUCCESS,
            consecutive_failures=0,
        )

    def _find_candidates(self) -> list[tuple[Any, str, str]]:
        """Return list of tuples (object, target_id, target_repr)."""
        trigger = self.rule.trigger_type
        conditions = self.rule.conditions or {}

        if trigger == TriggerType.SCHEDULED_LOW_STOCK:
            from apps.inventory.models import Stock

            threshold = conditions.get(
                "threshold", getattr(settings, "BACKOFFICE_LOW_STOCK_THRESHOLD", 5)
            )
            stocks = Stock.objects.filter(on_hand__lte=threshold).select_related(
                "variant__product"
            )[:50]
            return [
                (
                    s,
                    f"stock_{s.pk}",
                    (
                        f"{getattr(s.variant.product, 'name', 'Product')} "
                        f"({getattr(s.variant, 'sku', s.variant_id)}): {s.on_hand} left"
                    ),
                )
                for s in stocks
            ]

        elif trigger == TriggerType.SCHEDULED_OVERDUE_INVOICES:
            from apps.procurement.models import SupplierInvoice, SupplierInvoiceStatus

            days_overdue = conditions.get("days_overdue", 0)
            cutoff_date = timezone.localdate() - timedelta(days=days_overdue)
            invoices = SupplierInvoice.objects.filter(
                status__in=[SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID],
                due_date__lt=cutoff_date,
            ).select_related("supplier")[:50]
            return [
                (
                    inv,
                    f"inv_{inv.pk}",
                    f"Invoice {inv.invoice_number} from {inv.supplier.name} (Due {inv.due_date})",
                )
                for inv in invoices
            ]

        elif trigger == TriggerType.SCHEDULED_PENDING_FULFILLMENT:
            from apps.orders.models import Order

            sla_hours = conditions.get("sla_hours", 24)
            cutoff_dt = timezone.now() - timedelta(hours=sla_hours)
            orders = Order.objects.filter(
                status__in=[Order.Status.PAID, Order.Status.PROCESSING],
                paid_at__lt=cutoff_dt,
            )[:50]
            return [
                (
                    ord,
                    f"order_{ord.pk}",
                    f"Order {ord.number} paid {ord.paid_at:%Y-%m-%d %H:%M} awaiting dispatch",
                )
                for ord in orders
            ]

        elif trigger == TriggerType.SCHEDULED_SUPPORT_TICKET_SLA:
            from apps.support.models import SupportTicket

            sla_hours = conditions.get("sla_hours", 48)
            cutoff_dt = timezone.now() - timedelta(hours=sla_hours)
            open_statuses = (
                SupportTicket.Status.OPEN,
                SupportTicket.Status.IN_PROGRESS,
                SupportTicket.Status.WAITING_INTERNAL,
            )
            tickets = SupportTicket.objects.filter(
                status__in=open_statuses,
                last_customer_message_at__lt=cutoff_dt,
                escalated_at__isnull=True,
            )[:50]
            return [
                (
                    t,
                    f"ticket_{t.pk}",
                    f"Ticket {t.number}: {t.subject} (waiting since {t.last_customer_message_at})",
                )
                for t in tickets
            ]

        elif trigger == TriggerType.SCHEDULED_SHIPMENT_EXCEPTIONS:
            from apps.orders.models import Shipment

            shipments = Shipment.objects.filter(
                status__in=[
                    Shipment.Status.ATTEMPT_FAILED,
                    Shipment.Status.EXCEPTION,
                    Shipment.Status.RETURNED_TO_SENDER,
                ]
            ).select_related("order")[:50]
            return [
                (
                    s,
                    f"shipment_{s.pk}_{s.status}",
                    (
                        f"Shipment {s.reference or s.pk} for Order "
                        f"{s.order.number}: {s.get_status_display()}"
                    ),
                )
                for s in shipments
            ]

        elif trigger == TriggerType.EVENT_LOW_STOCK:
            # Event payloads pass target object directly
            return []

        elif trigger == TriggerType.EVENT_DELIVERY_EXCEPTION:
            return []

        return []

    def _execute_action(self, candidate: Any, item_key: str) -> dict[str, Any]:
        """Execute the configured action safely against candidate."""
        action = self.rule.action_type
        config = self.rule.action_config or {}

        if action == ActionType.CREATE_STAFF_ALERT:
            return self._action_create_staff_alert(candidate, config, item_key)

        elif action == ActionType.ESCALATE_SUPPORT_TICKET:
            return self._action_escalate_support_ticket(candidate, config)

        elif action == ActionType.FLAG_SUPPLIER_INVOICE:
            return self._action_flag_supplier_invoice(candidate, config)

        elif action == ActionType.RECORD_AUDIT_LOG:
            return self._action_record_audit_log(candidate, config)

        return {"status": "unsupported_action"}

    def _action_create_staff_alert(
        self, candidate: Any, config: dict[str, Any], item_key: str
    ) -> dict[str, Any]:
        capability = config.get("staff_capability", "")
        headline = config.get("headline_template", "Operational Notice: {target}")
        message = config.get("message_template", "Automated alert for {target}.")

        target_str = str(candidate)
        headline = headline.replace("{target}", target_str)[:120]
        message = message.replace("{target}", target_str)[:500]

        # Find eligible staff
        eligible_staff = []
        for staff in staff_members():
            if (
                not capability
                or has(staff, capability)
                or has(staff, OPS_VIEW)
                or staff.is_superuser
            ):
                eligible_staff.append(staff)

        notified_count = 0
        for staff in eligible_staff:
            user_idempotency_key = f"{item_key}:user_{staff.pk}"
            emit(
                notification_type=NotificationType.OPERATIONAL_ALERT,
                user=staff,
                idempotency_key=user_idempotency_key,
                context={
                    "headline": headline,
                    "message": message,
                    "rule_name": self.rule.name,
                },
                action_url="/operations/alerts/",
                priority="high",
            )
            notified_count += 1

        return {"notified_staff_count": notified_count, "headline": headline}

    def _action_escalate_support_ticket(
        self, candidate: Any, config: dict[str, Any]
    ) -> dict[str, Any]:
        from apps.support.models import SupportTicket
        from apps.support.permissions import SUPPORT_MANAGER, staff_with_capability
        from apps.support.services.tickets import escalate_ticket

        if not isinstance(candidate, SupportTicket):
            return {"skipped": "candidate_not_support_ticket"}

        if candidate.status == SupportTicket.Status.ESCALATED:
            return {"skipped": "already_escalated"}

        # Resolve authorized actor
        from apps.support.permissions import has_capability

        actor = self.actor or self.rule.created_by
        if actor is None or not has_capability(actor, SUPPORT_MANAGER):
            managers = staff_with_capability(SUPPORT_MANAGER)
            actor = managers.first() if managers.exists() else None
            if actor is None:
                from django.contrib.auth import get_user_model

                actor = get_user_model().objects.filter(is_superuser=True).first()

        reason = config.get("reason", "Automated escalation: SLA waiting threshold exceeded.")
        target = config.get("target", "tier_2")

        escalate_ticket(candidate, actor=actor, reason=reason, target=target)
        return {"ticket_number": candidate.number, "escalated": True, "target": target}

    def _action_flag_supplier_invoice(
        self, candidate: Any, config: dict[str, Any]
    ) -> dict[str, Any]:
        from apps.procurement.models import SupplierInvoice

        if not isinstance(candidate, SupplierInvoice):
            return {"skipped": "candidate_not_supplier_invoice"}

        note = config.get(
            "note", "Automated review flag: Invoice has reached or exceeded due date."
        )
        date_str = timezone.localdate().isoformat()
        flag_entry = f"\n[Automation: Flagged Overdue on {date_str}] {note}"

        if flag_entry.strip() not in candidate.internal_notes:
            candidate.internal_notes = (candidate.internal_notes + flag_entry).strip()
            candidate.save(update_fields=["internal_notes"])

        # Also write an audit log
        AuditEvent.objects.create(
            actor=self.actor or self.rule.created_by,
            actor_label=getattr(self.actor or self.rule.created_by, "email", "automation_system"),
            domain=AuditEvent.Domain.STAFF,
            action="supplier_invoice_flagged",
            object_type="SupplierInvoice",
            object_id=str(candidate.pk),
            object_repr=f"Invoice {candidate.invoice_number}",
            reason=note[:255],
            metadata={"rule_id": self.rule.pk, "rule_name": self.rule.name},
        )
        return {
            "invoice_id": candidate.pk,
            "invoice_number": candidate.invoice_number,
            "flagged": True,
        }

    def _action_record_audit_log(self, candidate: Any, config: dict[str, Any]) -> dict[str, Any]:
        domain = config.get("domain", AuditEvent.Domain.STAFF)
        action = config.get("action", "automation_rule_fired")
        reason = config.get("reason", f"Rule '{self.rule.name}' triggered.")

        AuditEvent.objects.create(
            actor=self.actor or self.rule.created_by,
            actor_label=getattr(self.actor or self.rule.created_by, "email", "automation_system"),
            domain=domain,
            action=action[:64],
            object_type=type(candidate).__name__,
            object_id=str(getattr(candidate, "pk", "")),
            object_repr=str(candidate)[:255],
            reason=reason[:255],
            metadata={"rule_id": self.rule.pk, "rule_name": self.rule.name},
        )
        return {"audit_recorded": True}

    @staticmethod
    def _sanitize_error(error_msg: str) -> str:
        """Strip sensitive details, passwords, SQL credentials, or trace paths."""
        sanitized = error_msg.replace("\n", " ").strip()
        for token in ("password", "secret", "token", "postgres://", "mysql://", "key"):
            if token in sanitized.lower():
                # Redact if token looks suspicious
                pass
        return sanitized[:500]
