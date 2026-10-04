"""FLASH Loop admin (Phase 13).

Moderation lives here: approving, rejecting, publishing, verifying
authenticity and completing trade-ins/recycling are staff actions
that go through the same services the API uses, so the admin cannot
sidestep the state machine either.

Seller-facing rows never show another customer's address, phone or
email beyond what admin itself already protects; ``search_fields``
only reaches the fields a moderator genuinely needs (slug, product
name, email of the account holder for support lookup).
"""

from __future__ import annotations

from django.contrib import admin

from apps.loop.models import (
    LoopCredit,
    LoopItem,
    LoopItemImage,
    RecycleRequest,
    ResaleListing,
    TradeInRequest,
)
from apps.loop.services import loop_items as loop_services
from apps.loop.services.errors import LoopError

__all__ = [
    "LoopCreditAdmin",
    "LoopItemAdmin",
    "LoopItemImageInline",
    "RecycleRequestAdmin",
    "ResaleListingAdmin",
    "TradeInRequestAdmin",
]


class LoopItemImageInline(admin.TabularInline):
    model = LoopItemImage
    extra = 0
    readonly_fields = ("position", "created_at")
    fields = ("image", "kind", "alt_text", "position")


@admin.register(LoopItem)
class LoopItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "type",
        "status",
        "condition",
        "authenticity_status",
        "owner_email",
        "product_name",
        "created_at",
    )
    list_filter = ("type", "status", "condition", "authenticity_status", "created_at")
    search_fields = ("=id", "user__email", "product__name", "title")
    readonly_fields = (
        "user",
        "product",
        "variant",
        "order_item",
        "closet_item",
        "authenticity_status",
        "reviewed_by",
        "reviewed_at",
        "listed_at",
        "sold_at",
        "created_at",
        "updated_at",
    )
    fields = (
        "user",
        "type",
        "status",
        "product",
        "variant",
        "order_item",
        "closet_item",
        "condition",
        "condition_notes",
        "title",
        "description",
        "asking_price",
        "authenticity_status",
        "moderation_note",
        "reviewed_by",
        "reviewed_at",
        "listed_at",
        "sold_at",
        "created_at",
        "updated_at",
    )
    inlines = (LoopItemImageInline,)
    actions = (
        "action_start_review",
        "action_approve_resale",
        "action_reject",
        "action_publish",
        "action_verify_authenticity",
    )
    date_hierarchy = "created_at"

    @admin.display(description="Owner")
    def owner_email(self, obj) -> str:
        return obj.user.email

    @admin.display(description="Product")
    def product_name(self, obj) -> str:
        if obj.product_id:
            return obj.product.name
        return "—"

    @admin.action(description="Start review (SUBMITTED → UNDER_REVIEW)")
    def action_start_review(self, request, queryset):
        self._run(request, queryset, loop_services.start_review)

    @admin.action(description="Approve for listing (UNDER_REVIEW → APPROVED)")
    def action_approve_resale(self, request, queryset):
        self._run(request, queryset, loop_services.approve_resale)

    @admin.action(description="Reject (→ REJECTED)")
    def action_reject(self, request, queryset):
        self._run(request, queryset, loop_services.reject_item, note="Rejected in admin.")

    @admin.action(description="Publish listing (APPROVED → LISTED)")
    def action_publish(self, request, queryset):
        self._run(request, queryset, loop_services.publish_listing)

    @admin.action(description="Mark authenticity VERIFIED")
    def action_verify_authenticity(self, request, queryset):
        self._run(
            request,
            queryset,
            lambda item, actor: loop_services.set_authenticity(
                item, actor=actor, status=LoopItem.AuthenticityStatus.VERIFIED
            ),
        )

    def _run(self, request, queryset, func, **kwargs) -> None:
        errors = []
        for item in queryset:
            try:
                func(item, actor=request.user, **kwargs)
            except LoopError as exc:
                errors.append(f"#{item.pk}: {exc.message}")
        if errors:
            self.message_user(
                request,
                "Some items could not be updated: " + "; ".join(errors[:10]),
                level="warning",
            )
        else:
            self.message_user(request, f"Updated {queryset.count()} item(s).")


@admin.register(ResaleListing)
class ResaleListingAdmin(admin.ModelAdmin):
    list_display = ("slug", "seller_email", "asking_price", "status", "listed_at", "sold_at")
    list_filter = ("status", "created_at")
    search_fields = ("=slug", "seller__email", "loop_item__product__name")
    readonly_fields = (
        "loop_item",
        "seller",
        "slug",
        "listed_at",
        "sold_at",
        "expired_at",
        "created_at",
        "updated_at",
    )
    fields = (
        "loop_item",
        "seller",
        "slug",
        "asking_price",
        "status",
        "listed_at",
        "sold_at",
        "expired_at",
        "created_at",
        "updated_at",
    )
    actions = ("action_activate", "action_cancel", "action_expire")

    @admin.display(description="Seller")
    def seller_email(self, obj) -> str:
        return obj.seller.email

    @admin.action(description="Activate (approve the listing)")
    def action_activate(self, request, queryset):
        for listing in queryset:
            try:
                if listing.status == ResaleListing.Status.PENDING_REVIEW:
                    loop_services.approve_resale(listing.loop_item, actor=request.user)
                    loop_services.publish_listing(listing.loop_item, actor=request.user)
            except LoopError as exc:
                self.message_user(request, f"{listing.slug}: {exc.message}", level="warning")

    @admin.action(description="Cancel listing")
    def action_cancel(self, request, queryset):
        for listing in queryset:
            if listing.status in (
                ResaleListing.Status.SOLD,
                ResaleListing.Status.CANCELLED,
            ):
                continue
            listing.status = ResaleListing.Status.CANCELLED
            listing.save(update_fields=["status", "updated_at"])

    @admin.action(description="Expire listing")
    def action_expire(self, request, queryset):
        from django.utils import timezone

        for listing in queryset:
            if listing.status != ResaleListing.Status.ACTIVE:
                continue
            listing.status = ResaleListing.Status.EXPIRED
            listing.expired_at = timezone.now()
            listing.save(update_fields=["status", "expired_at", "updated_at"])


@admin.register(TradeInRequest)
class TradeInRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "customer_email",
        "status",
        "estimated_credit",
        "final_credit",
        "reviewer_email",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("=id", "user__email", "loop_item__product__name")
    readonly_fields = (
        "loop_item",
        "user",
        "estimated_credit",
        "reviewed_at",
        "completed_at",
        "created_at",
        "updated_at",
    )
    fields = (
        "loop_item",
        "user",
        "status",
        "estimated_credit",
        "final_credit",
        "reviewer",
        "reviewer_note",
        "reviewed_at",
        "completed_at",
        "created_at",
        "updated_at",
    )
    actions = ("action_review", "action_offer", "action_complete")

    @admin.display(description="Customer")
    def customer_email(self, obj) -> str:
        return obj.user.email

    @admin.display(description="Reviewer")
    def reviewer_email(self, obj) -> str:
        return obj.reviewer.email if obj.reviewer else "—"

    @admin.action(description="Start review (SUBMITTED → UNDER_REVIEW)")
    def action_review(self, request, queryset):
        for row in queryset:
            try:
                loop_services.start_review(row.loop_item, actor=request.user)
            except LoopError as exc:
                self.message_user(request, f"#{row.pk}: {exc.message}", level="warning")

    @admin.action(description="Offer the estimated credit")
    def action_offer(self, request, queryset):
        for row in queryset:
            try:
                loop_services.start_review(row.loop_item, actor=request.user)
                loop_services.offer_trade_in(
                    row.loop_item, actor=request.user, final_credit=row.estimated_credit
                )
            except LoopError as exc:
                self.message_user(request, f"#{row.pk}: {exc.message}", level="warning")

    @admin.action(description="Complete and award credit")
    def action_complete(self, request, queryset):
        for row in queryset:
            try:
                credit = loop_services.complete_trade_in(row.loop_item, actor=request.user)
                self.message_user(request, f"#{row.pk}: awarded {credit.amount}.")
            except LoopError as exc:
                self.message_user(request, f"#{row.pk}: {exc.message}", level="warning")


@admin.register(RecycleRequest)
class RecycleRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "customer_email", "status", "material_category", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("=id", "user__email", "loop_item__product__name")
    readonly_fields = (
        "loop_item",
        "user",
        "impact",
        "received_at",
        "processed_at",
        "created_at",
        "updated_at",
    )
    fields = (
        "loop_item",
        "user",
        "status",
        "instructions",
        "material_category",
        "impact",
        "received_at",
        "processed_at",
        "created_at",
        "updated_at",
    )
    actions = ("action_accept", "action_reject", "action_complete")

    @admin.display(description="Customer")
    def customer_email(self, obj) -> str:
        return obj.user.email

    @admin.action(description="Accept request")
    def action_accept(self, request, queryset):
        for row in queryset:
            try:
                loop_services.accept_recycling(row.loop_item, actor=request.user)
            except LoopError as exc:
                self.message_user(request, f"#{row.pk}: {exc.message}", level="warning")

    @admin.action(description="Reject request")
    def action_reject(self, request, queryset):
        for row in queryset:
            try:
                loop_services.reject_recycling(
                    row.loop_item, actor=request.user, note="Rejected in admin."
                )
            except LoopError as exc:
                self.message_user(request, f"#{row.pk}: {exc.message}", level="warning")

    @admin.action(description="Complete (RECYCLE_COMPLETED)")
    def action_complete(self, request, queryset):
        for row in queryset:
            try:
                loop_services.complete_recycling(row.loop_item, actor=request.user)
            except LoopError as exc:
                self.message_user(request, f"#{row.pk}: {exc.message}", level="warning")


@admin.register(LoopCredit)
class LoopCreditAdmin(admin.ModelAdmin):
    list_display = ("reference", "user_email", "amount", "status", "awarded_at", "expires_at")
    list_filter = ("status", "awarded_at")
    search_fields = ("=reference", "user__email")
    readonly_fields = ("user", "amount", "reference", "loop_item", "trade_in_request", "awarded_at")
    fields = (
        "user",
        "amount",
        "reference",
        "status",
        "loop_item",
        "trade_in_request",
        "awarded_at",
        "expires_at",
    )

    @admin.display(description="Customer")
    def user_email(self, obj) -> str:
        return obj.user.email

    def has_add_permission(self, request) -> bool:
        # Credits are awarded by the service with an idempotent reference;
        # hand-adding one would bypass that.
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # The ledger is append-only: revoke instead of delete.
        return False
