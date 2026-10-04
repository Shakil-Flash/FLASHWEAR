"""Back Office models (Phase 16).

One table: the operational audit log. Everything else the back office shows already
exists in its own domain, and duplicating it here would create a second source of truth
for stock, points, moderation state or order status.

``AuditEvent`` is **append-only by construction**:

* :meth:`AuditEvent.save` refuses to rewrite a row that already exists, and
  :meth:`AuditEvent.delete` refuses to remove one -- ordinary staff therefore cannot edit
  history even if a view forgets to protect it;
* the actor is stored twice, once as a foreign key (so the log can be filtered by staff
  member while the account exists) and once as a snapshot of their email (so the entry
  still says *who* after the account is renamed or deleted);
* ``metadata`` is a small JSON bag for context such as ``{"delta": -3, "reason": ...}``.
  It is written by services, never by a client payload, and must never carry secrets,
  card data or credentials.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

__all__ = ["AuditEvent"]


class AuditEvent(models.Model):
    """One staff action, written once and never rewritten."""

    #: Operational areas, used for filtering ("show me everything the desk did today").
    class Domain(models.TextChoices):
        ORDERS = "orders", _("Orders")
        PAYMENTS = "payments", _("Payments")
        INVENTORY = "inventory", _("Inventory")
        CATALOG = "catalog", _("Catalogue")
        MARKETING = "marketing", _("Marketing")
        LOYALTY = "loyalty", _("FLASH Points")
        MODERATION = "moderation", _("Moderation")
        LOOP = "loop", _("FLASH Loop")
        SUPPORT = "support", _("Support")
        STAFF = "staff", _("Staff")

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="backoffice_audit_events",
        verbose_name=_("actor"),
    )
    actor_label = models.CharField(_("actor email"), max_length=255)
    domain = models.CharField(_("domain"), max_length=32, choices=Domain.choices, db_index=True)
    action = models.CharField(_("action"), max_length=64, db_index=True)
    object_type = models.CharField(_("object type"), max_length=64)
    object_id = models.CharField(_("object identifier"), max_length=64, blank=True)
    object_repr = models.CharField(_("object label"), max_length=255, blank=True)
    reason = models.CharField(_("reason"), max_length=255, blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    created_at = models.DateTimeField(_("created"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("audit event")
        verbose_name_plural = _("audit events")
        ordering = ("-created_at", "-pk")
        indexes = [
            models.Index(fields=["-created_at"], name="bo_audit_created_idx"),
            models.Index(fields=["domain", "-created_at"], name="bo_audit_domain_idx"),
            models.Index(fields=["object_type", "object_id"], name="bo_audit_object_idx"),
            models.Index(fields=["actor", "-created_at"], name="bo_audit_actor_idx"),
            models.Index(fields=["action", "-created_at"], name="bo_audit_action_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.object_repr or self.object_id}"

    def save(self, *args, **kwargs):
        """Create only: an existing row is history and history is not editable."""
        if self.pk is not None:
            raise ValidationError(_("Audit events are append-only."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Audit events are append-only."))
