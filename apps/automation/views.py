"""Views for Backoffice Business Automation management (Phase 56)."""

from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.automation.forms import AutomationRuleForm
from apps.automation.models import AutomationExecutionLog, AutomationRule
from apps.automation.services import evaluate_rule, toggle_rule
from apps.backoffice.models import AuditEvent
from apps.backoffice.permissions import (
    AUTOMATION_MANAGE,
    AUTOMATION_VIEW,
    backoffice_access,
)
from apps.backoffice.views.base import render_bo

PAGE_SIZE = 25


@backoffice_access(AUTOMATION_VIEW)
def rule_list(request):
    """List configured automations, status overview, and quick controls."""
    rules = AutomationRule.objects.select_related("created_by").order_by("-is_active", "name")
    recent_logs = AutomationExecutionLog.objects.select_related("rule").order_by("-executed_at")[
        :10
    ]

    today = timezone.localdate()
    today_start = timezone.make_aware(
        timezone.datetime.combine(today, timezone.datetime.min.time())
    )

    total_rules = rules.count()
    active_rules = rules.filter(is_active=True).count()
    runs_today = AutomationExecutionLog.objects.filter(executed_at__gte=today_start).count()
    failures_today = AutomationExecutionLog.objects.filter(
        executed_at__gte=today_start,
        status="failed",
    ).count()

    metrics = {
        "total_rules": total_rules,
        "active_rules": active_rules,
        "runs_today": runs_today,
        "failures_today": failures_today,
    }

    return render_bo(
        request,
        "backoffice/automation/list.html",
        active="automations",
        rules=rules,
        recent_logs=recent_logs,
        metrics=metrics,
    )


@backoffice_access(AUTOMATION_MANAGE)
def rule_create(request):
    """Create a new automation rule with strict server validation."""
    if request.method == "POST":
        form = AutomationRuleForm(request.POST)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.created_by = request.user
            rule.save()

            AuditEvent.objects.create(
                actor=request.user,
                actor_label=request.user.email,
                domain=AuditEvent.Domain.STAFF,
                action="automation_rule_created",
                object_type="AutomationRule",
                object_id=str(rule.pk),
                object_repr=rule.name,
                reason=f"Automation rule '{rule.name}' created.",
                metadata={
                    "rule_id": rule.pk,
                    "trigger": rule.trigger_type,
                    "action": rule.action_type,
                },
            )
            messages.success(
                request, _("Automation rule '%(name)s' created successfully.") % {"name": rule.name}
            )
            return redirect("backoffice:automations")
    else:
        form = AutomationRuleForm()

    return render_bo(
        request,
        "backoffice/automation/form.html",
        active="automations",
        form=form,
        title=_("Create Automation Rule"),
        is_create=True,
    )


@backoffice_access(AUTOMATION_MANAGE)
def rule_edit(request, pk: int):
    """Edit an existing automation rule with full validation."""
    rule = get_object_or_404(AutomationRule, pk=pk)
    if request.method == "POST":
        form = AutomationRuleForm(request.POST, instance=rule)
        if form.is_valid():
            rule = form.save()
            AuditEvent.objects.create(
                actor=request.user,
                actor_label=request.user.email,
                domain=AuditEvent.Domain.STAFF,
                action="automation_rule_updated",
                object_type="AutomationRule",
                object_id=str(rule.pk),
                object_repr=rule.name,
                reason=f"Automation rule '{rule.name}' modified.",
                metadata={
                    "rule_id": rule.pk,
                    "trigger": rule.trigger_type,
                    "action": rule.action_type,
                },
            )
            messages.success(
                request, _("Automation rule '%(name)s' updated successfully.") % {"name": rule.name}
            )
            return redirect("backoffice:automations")
    else:
        form = AutomationRuleForm(instance=rule)

    return render_bo(
        request,
        "backoffice/automation/form.html",
        active="automations",
        form=form,
        rule=rule,
        title=_("Edit Automation Rule: %(name)s") % {"name": rule.name},
        is_create=False,
    )


@require_POST
@backoffice_access(AUTOMATION_MANAGE)
def rule_toggle(request, pk: int):
    """Quick enable/disable switch for an automation rule."""
    rule = get_object_or_404(AutomationRule, pk=pk)
    toggle_rule(rule, actor=request.user)
    state = _("enabled") if rule.is_active else _("disabled")
    messages.success(
        request,
        _("Automation rule '%(name)s' was %(state)s.") % {"name": rule.name, "state": state},
    )
    return redirect("backoffice:automations")


@require_POST
@backoffice_access(AUTOMATION_MANAGE)
def rule_run(request, pk: int):
    """Manually trigger immediate execution of an automation rule."""
    rule = get_object_or_404(AutomationRule, pk=pk)
    log = evaluate_rule(rule, actor=request.user, force=True)

    AuditEvent.objects.create(
        actor=request.user,
        actor_label=request.user.email,
        domain=AuditEvent.Domain.STAFF,
        action="automation_rule_run_manually",
        object_type="AutomationRule",
        object_id=str(rule.pk),
        object_repr=rule.name,
        reason=f"Manual execution triggered by {request.user.email}.",
        metadata={"rule_id": rule.pk, "status": log.status, "affected": log.items_affected},
    )

    if log.status == "success":
        messages.success(
            request,
            _("Rule '%(name)s' executed: %(eval)d evaluated, %(aff)d affected (%(dur)dms).")
            % {
                "name": rule.name,
                "eval": log.items_evaluated,
                "aff": log.items_affected,
                "dur": log.duration_ms,
            },
        )
    elif log.status == "skipped":
        messages.info(
            request,
            _("Rule '%(name)s' skipped execution (cooldown active or no matching items).")
            % {"name": rule.name},
        )
    else:
        messages.error(
            request,
            _("Rule '%(name)s' failed execution: %(err)s")
            % {"name": rule.name, "err": log.error_summary or _("Unknown error")},
        )

    return redirect("backoffice:automations")


@backoffice_access(AUTOMATION_VIEW)
def execution_logs(request):
    """Filterable execution log history."""
    queryset = AutomationExecutionLog.objects.select_related("rule").order_by("-executed_at")

    rule_id = request.GET.get("rule")
    if rule_id and rule_id.isdigit():
        queryset = queryset.filter(rule_id=int(rule_id))

    status = request.GET.get("status")
    if status in ("success", "skipped", "failed"):
        queryset = queryset.filter(status=status)

    paginator = Paginator(queryset, PAGE_SIZE)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    rules = AutomationRule.objects.all().order_by("name")

    return render_bo(
        request,
        "backoffice/automation/logs.html",
        active="automations",
        page=page_obj,
        rules=rules,
        selected_rule=rule_id,
        selected_status=status,
    )


@backoffice_access(AUTOMATION_VIEW)
def log_detail(request, pk: int):
    """Detail view of a single automation execution log."""
    log = get_object_or_404(AutomationExecutionLog.objects.select_related("rule"), pk=pk)
    return render_bo(
        request,
        "backoffice/automation/log_detail.html",
        active="automations",
        log=log,
    )
