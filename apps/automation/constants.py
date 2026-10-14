"""Constants and allowlists for Business Automation (Phase 56).

Strictly defines supported triggers, conditions, actions, and capabilities.
Prevents arbitrary execution or unvalidated behavior.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.backoffice.permissions import (
    INVENTORY_MANAGE,
    INVENTORY_VIEW,
    INVOICES_MANAGE,
    INVOICES_VIEW,
    ORDERS_MANAGE,
    ORDERS_VIEW,
    SUPPORT_MANAGE,
    SUPPORT_VIEW,
)


class TriggerType(models.TextChoices):
    """Allowlist of supported automation triggers."""

    SCHEDULED_LOW_STOCK = "scheduled_low_stock", _("Scheduled: Low Inventory Scan")
    SCHEDULED_OVERDUE_INVOICES = (
        "scheduled_overdue_invoices",
        _("Scheduled: Overdue Supplier Invoices"),
    )
    SCHEDULED_PENDING_FULFILLMENT = (
        "scheduled_pending_fulfillment",
        _("Scheduled: Pending Fulfillment SLA"),
    )
    SCHEDULED_SUPPORT_TICKET_SLA = (
        "scheduled_support_ticket_sla",
        _("Scheduled: Support Ticket SLA Breach"),
    )
    SCHEDULED_SHIPMENT_EXCEPTIONS = (
        "scheduled_shipment_exceptions",
        _("Scheduled: Courier Delivery Exceptions"),
    )
    EVENT_LOW_STOCK = "event_low_stock", _("Event: Immediate Low Stock Detected")
    EVENT_DELIVERY_EXCEPTION = "event_delivery_exception", _("Event: Delivery Exception Reported")


class ActionType(models.TextChoices):
    """Allowlist of supported automation actions."""

    CREATE_STAFF_ALERT = "create_staff_alert", _("Create Staff Alert")
    ESCALATE_SUPPORT_TICKET = "escalate_support_ticket", _("Escalate Support Ticket")
    FLAG_SUPPLIER_INVOICE = "flag_supplier_invoice", _("Flag Supplier Invoice")
    RECORD_AUDIT_LOG = "record_audit_log", _("Record Backoffice Audit Log")


class ExecutionStatus(models.TextChoices):
    """Status outcomes for an automation evaluation."""

    SUCCESS = "success", _("Success")
    SKIPPED = "skipped", _("Skipped")
    FAILED = "failed", _("Failed")


# Mapping of trigger types to their permitted condition keys & default values
TRIGGER_CONDITION_SCHEMAS: dict[str, dict[str, type]] = {
    TriggerType.SCHEDULED_LOW_STOCK: {
        "threshold": int,
    },
    TriggerType.SCHEDULED_OVERDUE_INVOICES: {
        "days_overdue": int,
    },
    TriggerType.SCHEDULED_PENDING_FULFILLMENT: {
        "sla_hours": int,
    },
    TriggerType.SCHEDULED_SUPPORT_TICKET_SLA: {
        "sla_hours": int,
    },
    TriggerType.SCHEDULED_SHIPMENT_EXCEPTIONS: {
        "min_attempts": int,
    },
    TriggerType.EVENT_LOW_STOCK: {
        "threshold": int,
    },
    TriggerType.EVENT_DELIVERY_EXCEPTION: {
        "min_attempts": int,
    },
}

# Mapping of action types to their permitted config keys & types
ACTION_CONFIG_SCHEMAS: dict[str, dict[str, type]] = {
    ActionType.CREATE_STAFF_ALERT: {
        "staff_capability": str,
        "headline_template": str,
        "message_template": str,
    },
    ActionType.ESCALATE_SUPPORT_TICKET: {
        "reason": str,
        "target": str,
    },
    ActionType.FLAG_SUPPLIER_INVOICE: {
        "note": str,
    },
    ActionType.RECORD_AUDIT_LOG: {
        "domain": str,
        "action": str,
        "reason": str,
    },
}

# Allowed staff capabilities for staff alert routing
ALLOWED_STAFF_CAPABILITIES: tuple[str, ...] = (
    INVENTORY_VIEW,
    INVENTORY_MANAGE,
    ORDERS_VIEW,
    ORDERS_MANAGE,
    INVOICES_VIEW,
    INVOICES_MANAGE,
    SUPPORT_VIEW,
    SUPPORT_MANAGE,
)

# Compatibility: Which actions can be paired with which triggers
TRIGGER_ACTION_COMPATIBILITY: dict[str, tuple[str, ...]] = {
    TriggerType.SCHEDULED_LOW_STOCK: (
        ActionType.CREATE_STAFF_ALERT,
        ActionType.RECORD_AUDIT_LOG,
    ),
    TriggerType.SCHEDULED_OVERDUE_INVOICES: (
        ActionType.CREATE_STAFF_ALERT,
        ActionType.FLAG_SUPPLIER_INVOICE,
        ActionType.RECORD_AUDIT_LOG,
    ),
    TriggerType.SCHEDULED_PENDING_FULFILLMENT: (
        ActionType.CREATE_STAFF_ALERT,
        ActionType.RECORD_AUDIT_LOG,
    ),
    TriggerType.SCHEDULED_SUPPORT_TICKET_SLA: (
        ActionType.CREATE_STAFF_ALERT,
        ActionType.ESCALATE_SUPPORT_TICKET,
        ActionType.RECORD_AUDIT_LOG,
    ),
    TriggerType.SCHEDULED_SHIPMENT_EXCEPTIONS: (
        ActionType.CREATE_STAFF_ALERT,
        ActionType.RECORD_AUDIT_LOG,
    ),
    TriggerType.EVENT_LOW_STOCK: (
        ActionType.CREATE_STAFF_ALERT,
        ActionType.RECORD_AUDIT_LOG,
    ),
    TriggerType.EVENT_DELIVERY_EXCEPTION: (
        ActionType.CREATE_STAFF_ALERT,
        ActionType.RECORD_AUDIT_LOG,
    ),
}
