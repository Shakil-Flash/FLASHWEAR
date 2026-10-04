"""Queryset helpers for the drops app."""

from __future__ import annotations

from django.db import models

from .models import DropStatus


class FlashDropQuerySet(models.QuerySet):
    """Queryset for FlashDrop with derived-state filtering."""

    def active(self) -> FlashDropQuerySet:
        """Drops that are live or scheduled (not draft/ended/cancelled)."""
        return self.filter(status__in=[DropStatus.LIVE, DropStatus.SCHEDULED])

    def live(self) -> FlashDropQuerySet:
        """Drops that are currently live."""
        return self.filter(status=DropStatus.LIVE)

    def upcoming(self) -> FlashDropQuerySet:
        """Drops that are scheduled but not yet live."""
        return self.filter(status=DropStatus.SCHEDULED)

    def ended(self) -> FlashDropQuerySet:
        """Drops that have ended."""
        return self.filter(status=DropStatus.ENDED)

    def cancelled(self) -> FlashDropQuerySet:
        """Drops that have been cancelled."""
        return self.filter(status=DropStatus.CANCELLED)

    def visible_to(self, user) -> FlashDropQuerySet:
        """Drops visible to this user."""
        return self.filter(status__in=[DropStatus.LIVE, DropStatus.SCHEDULED])

    def with_storefront_data(self) -> FlashDropQuerySet:
        """Eager-load everything a drop landing page or product card touches."""
        return self.select_related("status").prefetch_related(
            "products__product",
            "products__variant__color",
            "products__variant__size",
        )

    def with_product_counts(self) -> FlashDropQuerySet:
        """Annotate each drop with its active product count."""
        from .models import DropProduct

        return self.annotate(
            active_product_count=models.Count(
                models.Prefetch(
                    "products",
                    queryset=DropProduct.objects.filter(is_active=True),
                    to_field="id",
                )
            )
        )
