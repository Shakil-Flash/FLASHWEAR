"""Delivery and shipment operations services (Phase 61).

Connects the dedicated FLASHWEAR Staff Portal to authoritative shipment,
delivery, tracking, and audit models without duplicating core business logic.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.backoffice.models import AuditEvent
from apps.backoffice.services.audit import record as record_audit_event
from apps.orders.fulfillment_services import (
    record_delivery_exception,
    update_tracking_event,
)
from apps.orders.models import (
    InvalidTransition,
    Shipment,
    ShipmentEvent,
)
from apps.staff.constants import (
    DELIVERIES_MANAGE,
    DELIVERIES_VIEW,
    StaffRole,
)
from apps.staff.dashboard_services import parse_date_range
from apps.staff.permissions import get_user_roles, has_staff_capability
from apps.staff.services import get_eligible_delivery_staff

logger = logging.getLogger("flashwear.staff.deliveries")
User = get_user_model()

PAGE_SIZE = 20

DELIVERY_FAILURE_REASONS: dict[str, Any] = {
    "recipient_unavailable": _("Recipient Unavailable / Not at Home"),
    "address_incomplete": _("Address Incomplete or Incorrect"),
    "recipient_declined": _("Recipient Declined Delivery"),
    "operational_issue": _("Operational Issue / Access Blocked / Severe Weather"),
}


def is_delivery_manager(user: Any) -> bool:
    """Return True if user has managerial delivery oversight (Owner, Ops Manager, or Superuser)."""
    if getattr(user, "is_superuser", False):
        return True
    roles = get_user_roles(user)
    return any(r in (StaffRole.OWNER, StaffRole.OPERATIONS_MANAGER) for r in roles)


def is_pure_delivery_worker(user: Any) -> bool:
    """Return True if user is a dedicated delivery staff worker without broader oversight."""
    if is_delivery_manager(user):
        return False
    roles = get_user_roles(user)
    has_worker_role = StaffRole.DELIVERY_STAFF in roles or "Delivery Staff" in [
        g.name for g in user.groups.all()
    ]
    has_broad_role = any(
        r in (StaffRole.ORDER_STAFF, StaffRole.SUPPORT_STAFF, StaffRole.READ_ONLY) for r in roles
    )
    return has_worker_role and not has_broad_role


# =============================================================================
# 1. Filtered Shipment Queries & Aggregations
# =============================================================================


def get_filtered_shipments(user: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Retrieve filtered, searched, and paginated shipments for the delivery workspace.

    Enforces role scoping: dedicated delivery staff only see parcels assigned to them.
    Managers and oversight staff view all team shipments.
    """
    if not has_staff_capability(user, DELIVERIES_VIEW):
        raise PermissionDenied(_("You do not have permission to view delivery operations."))

    pure_worker = is_pure_delivery_worker(user)

    shipments_qs = Shipment.objects.select_related(
        "order__user",
        "order__shipping_address",
        "assigned_to",
        "assigned_by",
        "dispatched_by",
    ).prefetch_related("items__order_item")

    # Dedicated delivery worker scope restriction
    if pure_worker:
        shipments_qs = shipments_qs.filter(assigned_to=user)

    # Search query
    search_q = (params.get("q") or "").strip()
    if search_q:
        shipments_qs = shipments_qs.filter(
            Q(reference__icontains=search_q)
            | Q(tracking_number__icontains=search_q)
            | Q(order__number__icontains=search_q)
            | Q(order__shipping_address__full_name__icontains=search_q)
            | Q(order__shipping_address__line1__icontains=search_q)
            | Q(order__shipping_address__city__icontains=search_q)
            | Q(order__shipping_address__postal_code__icontains=search_q)
            | Q(order__user__email__icontains=search_q)
        )

    # Status filter
    status_filter = (params.get("status") or "all").strip().lower()
    if status_filter == "ready":
        shipments_qs = shipments_qs.filter(
            status__in=[
                Shipment.Status.PENDING,
                Shipment.Status.PICKING,
                Shipment.Status.PACKED,
                Shipment.Status.PROCESSING,
            ]
        )
    elif status_filter == "shipped":
        shipments_qs = shipments_qs.filter(status=Shipment.Status.SHIPPED)
    elif status_filter == "in_transit":
        shipments_qs = shipments_qs.filter(status=Shipment.Status.IN_TRANSIT)
    elif status_filter == "out_for_delivery":
        shipments_qs = shipments_qs.filter(status=Shipment.Status.OUT_FOR_DELIVERY)
    elif status_filter == "delivered":
        shipments_qs = shipments_qs.filter(status=Shipment.Status.DELIVERED)
    elif status_filter == "exceptions":
        shipments_qs = shipments_qs.filter(
            Q(
                status__in=[
                    Shipment.Status.ATTEMPT_FAILED,
                    Shipment.Status.EXCEPTION,
                    Shipment.Status.RETURNED_TO_SENDER,
                ]
            )
            | Q(requires_manager_review=True)
        )
    elif status_filter != "all" and status_filter in dict(Shipment.Status.choices):
        shipments_qs = shipments_qs.filter(status=status_filter)

    # Assigned staff filter (only managers can filter across other workers)
    assigned_filter = (params.get("assigned_to") or "").strip()
    if assigned_filter and not pure_worker:
        if assigned_filter == "unassigned":
            shipments_qs = shipments_qs.filter(assigned_to__isnull=True)
        elif assigned_filter == "me":
            shipments_qs = shipments_qs.filter(assigned_to=user)
        elif assigned_filter.isdigit():
            shipments_qs = shipments_qs.filter(assigned_to_id=int(assigned_filter))

    # Flagged for manager review filter
    if params.get("needs_review") in ("1", "true", "True"):
        shipments_qs = shipments_qs.filter(requires_manager_review=True)

    # Date range filter
    date_filter = params.get("date_range", "all")
    start_date, end_date = parse_date_range(date_filter)
    if start_date and end_date:
        shipments_qs = shipments_qs.filter(created_at__gte=start_date, created_at__lte=end_date)

    # Tab badge aggregations (computed on base scoped queryset)
    base_scope_qs = Shipment.objects.all()
    if pure_worker:
        base_scope_qs = base_scope_qs.filter(assigned_to=user)

    counts = base_scope_qs.aggregate(
        all_count=Count("pk"),
        ready_count=Count(
            "pk",
            filter=Q(
                status__in=[
                    Shipment.Status.PENDING,
                    Shipment.Status.PICKING,
                    Shipment.Status.PACKED,
                    Shipment.Status.PROCESSING,
                ]
            ),
        ),
        out_for_delivery_count=Count("pk", filter=Q(status=Shipment.Status.OUT_FOR_DELIVERY)),
        exceptions_count=Count(
            "pk",
            filter=Q(
                status__in=[
                    Shipment.Status.ATTEMPT_FAILED,
                    Shipment.Status.EXCEPTION,
                    Shipment.Status.RETURNED_TO_SENDER,
                ]
            )
            | Q(requires_manager_review=True),
        ),
        delivered_count=Count("pk", filter=Q(status=Shipment.Status.DELIVERED)),
    )

    # Sorting
    sort_by = params.get("sort", "-created_at")
    allowed_sorts = {
        "-created_at": "-created_at",
        "created_at": "created_at",
        "status": "status",
        "reference": "reference",
        "estimated_delivery": "estimated_delivery",
    }
    order_field = allowed_sorts.get(sort_by, "-created_at")
    shipments_qs = shipments_qs.order_by(order_field)

    # Pagination
    paginator = Paginator(shipments_qs, PAGE_SIZE)
    page_num = params.get("page", 1)
    try:
        page_obj = paginator.page(page_num)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages if paginator.num_pages > 0 else 1)

    eligible_staff = get_eligible_delivery_staff() if not pure_worker else []

    return {
        "page_obj": page_obj,
        "shipments": page_obj.object_list,
        "paginator": paginator,
        "counts": counts,
        "pure_worker": pure_worker,
        "is_manager": is_delivery_manager(user),
        "eligible_staff": eligible_staff,
        "current_filters": {
            "q": search_q,
            "status": status_filter,
            "assigned_to": assigned_filter,
            "date_range": date_filter,
            "sort": sort_by,
            "needs_review": params.get("needs_review", ""),
        },
    }


# =============================================================================
# 2. Detailed Shipment Inspection & Timeline
# =============================================================================


def get_shipment_details(shipment_id: int, user: Any) -> dict[str, Any]:
    """Retrieve full shipment context with address snapshot, timeline, and actions.

    Enforces IDOR security: delivery workers cannot view other workers' shipments.
    """
    if not has_staff_capability(user, DELIVERIES_VIEW):
        raise PermissionDenied(_("You do not have permission to view delivery operations."))

    try:
        shipment = (
            Shipment.objects.select_related(
                "order__user",
                "order__shipping_address",
                "assigned_to",
                "assigned_by",
                "picked_by",
                "packed_by",
                "dispatched_by",
            )
            .prefetch_related(
                "items__order_item__variant__product",
                "events__actor",
            )
            .get(pk=shipment_id)
        )
    except Shipment.DoesNotExist as err:
        raise ValidationError(_("Shipment not found.")) from err

    # IDOR Protection: pure delivery worker can only view shipments assigned to them
    if is_pure_delivery_worker(user):
        if shipment.assigned_to_id != user.pk:
            raise PermissionDenied(
                _("You are not authorized to view shipments assigned to other workers.")
            )

    # Construct chronological unified timeline (ShipmentEvents + AuditEvents)
    timeline: list[dict[str, Any]] = []

    for event in shipment.events.all():
        timeline.append(
            {
                "timestamp": event.occurred_at or event.created_at,
                "actor": event.actor,
                "actor_name": (
                    event.actor.get_full_name() or event.actor.email
                    if event.actor
                    else event.source.title()
                ),
                "event_type": event.event_type,
                "title": dict(Shipment.Status.choices).get(
                    event.event_type, event.event_type.title()
                ),
                "location": event.location,
                "note": event.note,
                "metadata": event.metadata,
                "source": event.source,
                "is_audit": False,
            }
        )

    # Include backoffice operational audit rows
    audit_events = AuditEvent.objects.filter(
        domain="orders",
        object_id=str(shipment.pk),
    ).select_related("actor")

    for a in audit_events:
        timeline.append(
            {
                "timestamp": a.created_at,
                "actor": a.actor,
                "actor_name": a.actor_label
                or (a.actor.get_full_name() or a.actor.email if a.actor else "System"),
                "event_type": a.action,
                "title": f"Audit: {a.action}",
                "location": "",
                "note": a.reason or a.metadata.get("note", ""),
                "metadata": a.metadata,
                "source": "audit",
                "is_audit": True,
            }
        )

    timeline.sort(key=lambda x: x["timestamp"], reverse=True)

    # Permission flags
    can_manage = has_staff_capability(user, DELIVERIES_MANAGE)
    manager = is_delivery_manager(user)
    is_assignee = shipment.assigned_to_id == user.pk

    allowed_next_statuses = [
        (s, dict(Shipment.Status.choices).get(s, s))
        for s in Shipment.ALLOWED_TRANSITIONS.get(shipment.status, ())
        if s != Shipment.Status.CANCELLED
    ]

    can_assign = manager and shipment.can_be_assigned
    can_update_status = can_manage and (manager or is_assignee) and len(allowed_next_statuses) > 0
    can_record_attempt = (
        can_manage
        and (manager or is_assignee)
        and shipment.status
        in (
            Shipment.Status.SHIPPED,
            Shipment.Status.IN_TRANSIT,
            Shipment.Status.OUT_FOR_DELIVERY,
            Shipment.Status.ATTEMPT_FAILED,
            Shipment.Status.EXCEPTION,
        )
    )
    can_record_pod = (
        can_manage
        and (manager or is_assignee)
        and shipment.status
        in (
            Shipment.Status.SHIPPED,
            Shipment.Status.IN_TRANSIT,
            Shipment.Status.OUT_FOR_DELIVERY,
            Shipment.Status.ATTEMPT_FAILED,
        )
    )
    can_generate_otp = (
        can_manage
        and (manager or is_assignee)
        and shipment.status
        in (
            Shipment.Status.SHIPPED,
            Shipment.Status.IN_TRANSIT,
            Shipment.Status.OUT_FOR_DELIVERY,
        )
    )

    eligible_staff = get_eligible_delivery_staff() if can_assign else []

    return {
        "shipment": shipment,
        "order": shipment.order,
        "items": shipment.items.all(),
        "timeline": timeline,
        "allowed_next_statuses": allowed_next_statuses,
        "can_assign": can_assign,
        "can_update_status": can_update_status,
        "can_record_attempt": can_record_attempt,
        "can_record_pod": can_record_pod,
        "can_generate_otp": can_generate_otp,
        "is_manager": manager,
        "is_assignee": is_assignee,
        "eligible_staff": eligible_staff,
    }


# =============================================================================
# 3. Shipment Assignment Actions
# =============================================================================


@transaction.atomic
def assign_shipment_action(
    shipment_id: int,
    *,
    staff_user_id: int,
    manager: Any,
    assignment_note: str = "",
) -> Shipment:
    """Assign or reassign an active shipment to an eligible delivery worker.

    Restricted to managers (Owner or Operations Manager).
    """
    if not has_staff_capability(manager, DELIVERIES_MANAGE) or not is_delivery_manager(manager):
        raise PermissionDenied(_("Only delivery operations managers can assign shipments."))

    try:
        shipment = Shipment.objects.select_for_update().get(pk=shipment_id)
    except Shipment.DoesNotExist as err:
        raise ValidationError(_("Shipment not found.")) from err

    if not shipment.can_be_assigned:
        raise ValidationError(
            _("Shipment %(ref)s is in status '%(status)s' and cannot be assigned.")
            % {
                "ref": shipment.reference or f"#{shipment.pk}",
                "status": shipment.get_status_display(),
            }
        )

    try:
        target_staff = User.objects.get(pk=staff_user_id)
    except User.DoesNotExist as err:
        raise ValidationError(_("Selected staff member does not exist.")) from err

    if not target_staff.is_active:
        raise ValidationError(_("Cannot assign shipment to an inactive staff account."))

    # Target staff must have delivery capability or role
    eligible_ids = set(get_eligible_delivery_staff().values_list("pk", flat=True))
    if target_staff.pk not in eligible_ids:
        raise ValidationError(_("Selected staff member is not authorized for delivery operations."))

    previous_assignee = shipment.assigned_to
    shipment.assigned_to = target_staff
    shipment.assigned_by = manager
    shipment.assigned_at = timezone.now()
    shipment.save(update_fields=["assigned_to", "assigned_by", "assigned_at", "updated_at"])

    # Record tracking event
    actor_label = manager.get_full_name() or manager.email
    target_label = target_staff.get_full_name() or target_staff.email
    action_name = "shipment.reassigned" if previous_assignee else "shipment.assigned"

    ShipmentEvent.objects.create(
        shipment=shipment,
        event_type=shipment.status,
        actor=manager,
        source=ShipmentEvent.Source.STAFF,
        note=assignment_note
        or _("Assigned to %(staff)s by %(manager)s.")
        % {
            "staff": target_label,
            "manager": actor_label,
        },
        metadata={
            "assigned_to_id": target_staff.pk,
            "assigned_to_email": target_staff.email,
            "previous_assignee_id": previous_assignee.pk if previous_assignee else None,
            "note": assignment_note,
        },
    )

    record_audit_event(
        actor=manager,
        domain="orders",
        action=action_name,
        object_type="shipment",
        object_id=str(shipment.pk),
        object_repr=f"Shipment {shipment.reference or shipment.pk}",
        reason=assignment_note,
        metadata={
            "assigned_to": target_staff.email,
            "previous_assignee": previous_assignee.email if previous_assignee else None,
            "shipment_reference": shipment.reference,
        },
    )

    logger.info("Shipment %s assigned to %s by %s", shipment.pk, target_staff.email, manager.email)
    return shipment


# =============================================================================
# 4. Status Progression & Checkpoints
# =============================================================================


@transaction.atomic
def update_delivery_status_action(
    shipment_id: int,
    *,
    target_status: str,
    location: str = "",
    note: str = "",
    actor: Any,
) -> Shipment:
    """Advance shipment status along allowed transit transitions.

    Enforces permission and worker assignment.
    """
    if not has_staff_capability(actor, DELIVERIES_MANAGE):
        raise PermissionDenied(_("You do not have permission to manage deliveries."))

    try:
        shipment = Shipment.objects.select_for_update().get(pk=shipment_id)
    except Shipment.DoesNotExist as err:
        raise ValidationError(_("Shipment not found.")) from err

    # Delivery workers can only advance their own assigned shipments
    if not is_delivery_manager(actor) and shipment.assigned_to_id != actor.pk:
        raise PermissionDenied(
            _("You are not authorized to update shipments assigned to other workers.")
        )

    # Idempotent return
    if shipment.status == target_status:
        return shipment

    if not shipment.can_transition_to(target_status):
        raise ValidationError(
            _("Cannot move shipment %(ref)s from '%(from)s' to '%(to)s'.")
            % {
                "ref": shipment.reference or f"#{shipment.pk}",
                "from": shipment.get_status_display(),
                "to": target_status,
            }
        )

    # When transitioning to OUT_FOR_DELIVERY, generate a confirmation OTP if not yet generated
    if target_status == Shipment.Status.OUT_FOR_DELIVERY and not shipment.pod_otp_hash:
        _generate_otp_internal(shipment)

    try:
        updated = update_tracking_event(
            shipment,
            event_type=target_status,
            status=target_status,
            location=location,
            note=note,
            actor=actor,
            source=ShipmentEvent.Source.STAFF,
        )
    except InvalidTransition as exc:
        raise ValidationError(str(exc)) from exc

    record_audit_event(
        actor=actor,
        domain="orders",
        action="shipment.tracking_event",
        object_type="shipment",
        object_id=str(shipment.pk),
        object_repr=f"Shipment {shipment.reference or shipment.pk}",
        reason=note,
        metadata={
            "target_status": target_status,
            "location": location,
            "shipment_reference": shipment.reference,
        },
    )
    return updated


# =============================================================================
# 5. Delivery Attempts & Exception Handling
# =============================================================================


@transaction.atomic
def record_delivery_attempt_action(
    shipment_id: int,
    *,
    failure_reason: str,
    location: str = "",
    note: str = "",
    next_attempt_date: date | None = None,
    requires_manager_review: bool = False,
    actor: Any,
) -> Shipment:
    """Record an unsuccessful delivery attempt with failure reason and next attempt schedule."""
    if not has_staff_capability(actor, DELIVERIES_MANAGE):
        raise PermissionDenied(_("You do not have permission to record delivery attempts."))

    try:
        shipment = Shipment.objects.select_for_update().get(pk=shipment_id)
    except Shipment.DoesNotExist as err:
        raise ValidationError(_("Shipment not found.")) from err

    if not is_delivery_manager(actor) and shipment.assigned_to_id != actor.pk:
        raise PermissionDenied(_("You are not authorized to record attempts for this shipment."))

    if shipment.is_in_terminal_state:
        raise ValidationError(_("Cannot record an attempt on a completed or cancelled shipment."))

    # Increment attempts count
    shipment.delivery_attempts_count += 1

    # Repeated attempts (>= 2) automatically flag for manager review
    if requires_manager_review or shipment.delivery_attempts_count >= 2:
        shipment.requires_manager_review = True

    if next_attempt_date:
        shipment.next_attempt_date = next_attempt_date

    shipment.save(
        update_fields=[
            "delivery_attempts_count",
            "requires_manager_review",
            "next_attempt_date",
            "updated_at",
        ]
    )

    reason_display = DELIVERY_FAILURE_REASONS.get(failure_reason, failure_reason)
    full_note = note or _("Delivery attempt failed: %(reason)s") % {"reason": reason_display}

    updated = record_delivery_exception(
        shipment,
        staff_user=actor,
        exception_type=Shipment.Status.ATTEMPT_FAILED,
        reason=failure_reason,
        note=full_note,
        location=location,
    )

    record_audit_event(
        actor=actor,
        domain="orders",
        action="shipment.attempt",
        object_type="shipment",
        object_id=str(shipment.pk),
        object_repr=f"Shipment {shipment.reference or shipment.pk}",
        reason=full_note,
        metadata={
            "failure_reason": failure_reason,
            "attempts": shipment.delivery_attempts_count,
            "next_attempt": str(next_attempt_date) if next_attempt_date else None,
            "requires_manager_review": shipment.requires_manager_review,
            "location": location,
        },
    )
    return updated


# =============================================================================
# 6. Proof of Delivery (POD) & Verification
# =============================================================================


def _hash_otp(shipment_id: int, otp_code: str) -> str:
    """Derive salted HMAC-style SHA-256 digest of raw OTP code."""
    salt = settings.SECRET_KEY
    payload = f"{shipment_id}:{otp_code.strip()}:{salt}".encode()
    return hashlib.sha256(payload).hexdigest()


def _generate_otp_internal(shipment: Shipment) -> str:
    """Generate and store salted 6-digit confirmation code on shipment instance."""
    raw_otp = f"{secrets.randbelow(900000) + 100000:06d}"
    shipment.pod_otp_hash = _hash_otp(shipment.pk, raw_otp)
    shipment.pod_otp_created_at = timezone.now()
    shipment.pod_otp_expires_at = timezone.now() + timedelta(hours=48)
    shipment.pod_otp_attempts = 0
    shipment.save(
        update_fields=[
            "pod_otp_hash",
            "pod_otp_created_at",
            "pod_otp_expires_at",
            "pod_otp_attempts",
            "updated_at",
        ]
    )
    return raw_otp


@transaction.atomic
def generate_delivery_otp_action(shipment_id: int, *, actor: Any) -> tuple[Shipment, str]:
    """Generate or regenerate a customer confirmation OTP code for delivery handoff."""
    if not has_staff_capability(actor, DELIVERIES_MANAGE):
        raise PermissionDenied(_("You do not have permission to generate delivery OTP codes."))

    try:
        shipment = Shipment.objects.select_for_update().get(pk=shipment_id)
    except Shipment.DoesNotExist as err:
        raise ValidationError(_("Shipment not found.")) from err

    if not is_delivery_manager(actor) and shipment.assigned_to_id != actor.pk:
        raise PermissionDenied(_("You are not authorized to generate OTP for this shipment."))

    if shipment.is_in_terminal_state:
        raise ValidationError(_("Cannot generate OTP for completed or cancelled shipment."))

    raw_otp = _generate_otp_internal(shipment)

    ShipmentEvent.objects.create(
        shipment=shipment,
        event_type=shipment.status,
        actor=actor,
        source=ShipmentEvent.Source.STAFF,
        note=_("Secure delivery confirmation code generated."),
        metadata={"expires_at": shipment.pod_otp_expires_at.isoformat()},
    )

    logger.info("Generated delivery OTP for shipment %s by actor %s", shipment.pk, actor)
    return shipment, raw_otp


def record_proof_of_delivery_action(
    shipment_id: int,
    *,
    pod_type: str,
    otp_code: str = "",
    recipient_name: str = "",
    notes: str = "",
    actor: Any,
) -> Shipment:
    """Validate proof of delivery and transition parcel and order to DELIVERED."""
    if not has_staff_capability(actor, DELIVERIES_MANAGE):
        raise PermissionDenied(_("You do not have permission to verify proof of delivery."))

    try:
        shipment = Shipment.objects.select_for_update().get(pk=shipment_id)
    except Shipment.DoesNotExist as err:
        raise ValidationError(_("Shipment not found.")) from err

    if not is_delivery_manager(actor) and shipment.assigned_to_id != actor.pk:
        raise PermissionDenied(_("You are not authorized to deliver this shipment."))

    if shipment.status == Shipment.Status.DELIVERED:
        return shipment

    if not shipment.can_transition_to(Shipment.Status.DELIVERED):
        raise ValidationError(
            _("Cannot deliver shipment %(ref)s from current status '%(status)s'.")
            % {
                "ref": shipment.reference or f"#{shipment.pk}",
                "status": shipment.get_status_display(),
            }
        )

    # 1. Verification of OTP
    if pod_type == "otp":
        if not shipment.pod_otp_hash:
            raise ValidationError(
                _(
                    "No delivery confirmation code has been generated for this parcel. "
                    "Generate one first or use recipient sign-off."
                )
            )

        if shipment.pod_otp_expires_at and timezone.now() > shipment.pod_otp_expires_at:
            raise ValidationError(
                _("The confirmation code has expired. Please generate a new code.")
            )

        if shipment.pod_otp_attempts >= 5:
            with transaction.atomic():
                shipment.requires_manager_review = True
                shipment.save(update_fields=["requires_manager_review", "updated_at"])
            raise ValidationError(
                _(
                    "Too many failed code attempts. "
                    "Parcel has been locked and flagged for manager review."
                )
            )

        candidate_hash = _hash_otp(shipment.pk, otp_code)
        if not secrets.compare_digest(candidate_hash, shipment.pod_otp_hash):
            with transaction.atomic():
                shipment.pod_otp_attempts += 1
                if shipment.pod_otp_attempts >= 5:
                    shipment.requires_manager_review = True
                shipment.save(
                    update_fields=["pod_otp_attempts", "requires_manager_review", "updated_at"]
                )
            remaining = max(0, 5 - shipment.pod_otp_attempts)
            raise ValidationError(
                _("Invalid confirmation code. %(rem)d attempt(s) remaining.") % {"rem": remaining}
            )

    # 2. Verification of Recipient Sign-Off
    elif pod_type == "recipient_signoff":
        if not recipient_name or len(recipient_name.strip()) < 2:
            raise ValidationError(
                _("A valid recipient name is required for manual delivery sign-off.")
            )
    else:
        raise ValidationError(_("Invalid proof of delivery verification method."))

    with transaction.atomic():
        # Save POD details
        now = timezone.now()
        shipment.pod_type = pod_type
        shipment.pod_recipient_name = recipient_name.strip()
        shipment.pod_notes = notes.strip()
        shipment.pod_verified_at = now
        shipment.delivered_at = now
        shipment.save(
            update_fields=[
                "pod_type",
                "pod_recipient_name",
                "pod_notes",
                "pod_verified_at",
                "delivered_at",
                "updated_at",
            ]
        )

        # Move status to DELIVERED and sync order
        note_msg = (
            _("Delivered with verified code (OTP). Handover notes: %(notes)s")
            % {"notes": notes or "N/A"}
            if pod_type == "otp"
            else _("Delivered and signed by %(recipient)s. Notes: %(notes)s")
            % {"recipient": recipient_name, "notes": notes or "N/A"}
        )

        updated = update_tracking_event(
            shipment,
            event_type=Shipment.Status.DELIVERED,
            status=Shipment.Status.DELIVERED,
            location=shipment.order.shipping_address.city
            if shipment.order.shipping_address
            else "",
            note=note_msg,
            actor=actor,
            source=ShipmentEvent.Source.STAFF,
            metadata={
                "pod_type": pod_type,
                "recipient_name": recipient_name,
                "verified_at": now.isoformat(),
            },
        )

        record_audit_event(
            actor=actor,
            domain="orders",
            action="shipment.pod_verified",
            object_type="shipment",
            object_id=str(shipment.pk),
            object_repr=f"Shipment {shipment.reference or shipment.pk}",
            reason=note_msg,
            metadata={
                "pod_type": pod_type,
                "recipient_name": recipient_name,
                "shipment_reference": shipment.reference,
                "status": Shipment.Status.DELIVERED,
            },
        )

    logger.info("Shipment %s confirmed DELIVERED via %s by %s", shipment.pk, pod_type, actor)
    return updated
