"""Governance screens: the audit log and staff roles (Phase 16).

The audit log is a *read*: :class:`apps.backoffice.models.AuditEvent` refuses to update
or delete itself, so the only thing this screen can do is filter, sort and page rows.
The staff screen is the one place a back-office group is granted, and it writes an audit
row for every change -- including a removal, because taking access away is as much a
privilege change as giving it.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.backoffice import selectors
from apps.backoffice.forms import StaffRoleForm
from apps.backoffice.permissions import (
    AUDIT_VIEW,
    BACKOFFICE_GROUPS,
    STAFF_MANAGE,
    backoffice_access,
    staff_members,
)
from apps.backoffice.services import operations
from apps.backoffice.views.base import range_from_request, render_bo

__all__ = ["audit", "staff", "staff_update"]


@backoffice_access(AUDIT_VIEW)
def audit(request):
    """``/operations/audit/`` -- who did what, newest first, filters from a fixed menu."""
    date_range = range_from_request(request)
    queryset = selectors.audit_events(
        q=request.GET.get("q", ""),
        actor=request.GET.get("actor", ""),
        action=request.GET.get("action", ""),
        domain=request.GET.get("domain", ""),
        date_range=date_range,
        sort=request.GET.get("sort", ""),
    )
    return render_bo(
        request,
        "backoffice/audit.html",
        active="audit",
        page=selectors.paginate(request, queryset),
        current_range=date_range.key,
        action_choices=[(action, action) for action in selectors.AUDIT_ACTIONS],
        domain_choices=[(value, label) for value, label in _domain_choices()],
        filters={
            "q": request.GET.get("q", ""),
            "actor": request.GET.get("actor", ""),
            "action": request.GET.get("action", ""),
            "domain": request.GET.get("domain", ""),
            "sort": request.GET.get("sort", ""),
        },
    )


def _domain_choices():
    from apps.backoffice.models import AuditEvent

    return AuditEvent.Domain.choices


@backoffice_access(STAFF_MANAGE)
def staff(request):
    """``/operations/staff/`` -- active staff accounts and the groups they hold.

    Groups come from the prefetch cache (``staff_members`` selects them), so a page of
    thirty operators costs one query, not thirty-one.
    """
    owned = set(BACKOFFICE_GROUPS)
    members = [
        {
            "user": user,
            "groups": {
                group.name for group in user.groups.all() if group.name in owned
            },
        }
        for user in staff_members()
    ]
    return render_bo(
        request,
        "backoffice/staff.html",
        active="staff",
        members=members,
        groups=BACKOFFICE_GROUPS,
        role_form=StaffRoleForm(),
    )


@require_POST
@backoffice_access(STAFF_MANAGE)
def staff_update(request, pk: int):
    """Grant or revoke back-office groups for one account, then log the change."""
    User = get_user_model()
    user = get_object_or_404(User.objects.filter(is_active=True, is_staff=True), pk=pk)
    form = StaffRoleForm(request.POST)
    if not form.is_valid():
        messages.error(request, "That role change is not available.")
        return redirect("backoffice:staff")

    try:
        changed = operations.set_staff_groups(
            user=user,
            add=list(form.cleaned_data["add"]),
            remove=list(form.cleaned_data["remove"]),
            actor=request.user,
        )
    except operations.DOMAIN_ERRORS as exc:
        messages.error(request, operations.error_message(exc))
    else:
        if changed:
            messages.success(request, f"{user.email}: {', '.join(changed)} updated.")
        else:
            messages.info(request, f"{user.email}: no change requested.")
    return redirect("backoffice:staff")
