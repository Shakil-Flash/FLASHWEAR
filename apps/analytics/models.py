"""Analytics models: the event log and per-visitor attribution.

Both tables are append-mostly by design. ``Event`` mirrors the idempotency pattern the
payments app proved (a unique key scoped by a partial constraint, ``""`` meaning "no key"),
so a retried webhook or a double-submitted checkout cannot double-count a conversion.

Nothing here stores raw IP addresses, user agents, payment data or free-text form bodies:
the identity is the visitor id / session key / user the shop already holds, and metadata is
scrubbed on write (see ``apps.analytics.services``).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class Event(models.Model):
    """One business event, recorded server-side at the moment the action completed."""

    class Name(models.TextChoices):
        # Catalogue / discovery
        PRODUCT_VIEW = "product_view", _("product view")
        PRODUCT_SEARCH = "product_search", _("product search")
        FILTER_USED = "filter_used", _("filter used")
        RECOMMENDATION_VIEW = "recommendation_view", _("recommendation view")
        # Phase 21 wired the click endpoint (Complete the Look / Smart Outfit links).
        RECOMMENDATION_CLICKED = "recommendation_clicked", _("recommendation clicked")
        # Shop
        CART_ADD = "cart_add", _("cart add")
        CART_REMOVE = "cart_remove", _("cart remove")
        WISHLIST_ADD = "wishlist_add", _("wishlist add")
        WISHLIST_REMOVE = "wishlist_remove", _("wishlist remove")
        CHECKOUT_STARTED = "checkout_started", _("checkout started")
        CHECKOUT_COMPLETED = "checkout_completed", _("checkout completed")
        PURCHASE = "purchase", _("purchase")
        # Drops / creator / loop / quests / support
        DROP_VIEW = "drop_view", _("drop view")
        DROP_PURCHASE = "drop_purchase", _("drop purchase")
        # Emitted once apps.creator is wired into INSTALLED_APPS (same as above).
        CREATOR_CONTENT_VIEW = "creator_content_view", _("creator content view")
        LOOP_ITEM_VIEW = "loop_item_view", _("loop item view")
        LOOP_ITEM_ACTION = "loop_item_action", _("loop item action")
        QUEST_VIEW = "quest_view", _("quest view")
        QUEST_COMPLETION = "quest_completion", _("quest completion")
        SUPPORT_TICKET_CREATED = "support_ticket_created", _("support ticket created")
        # Fashion intelligence (Phase 21)
        OUTFIT_GENERATED = "outfit_generated", _("outfit generated")
        OUTFIT_SAVED = "outfit_saved", _("outfit saved")
        OUTFIT_ITEM_REPLACED = "outfit_item_replaced", _("outfit item replaced")
        COMPLETE_LOOK_VIEWED = "complete_look_viewed", _("complete look viewed")
        STYLE_GOAL_SELECTED = "style_goal_selected", _("style goal selected")
        MOOD_OUTFIT_GENERATED = "mood_outfit_generated", _("mood outfit generated")
        # Visual product discovery (Phase 22)
        IMAGE_SEARCH_STARTED = "image_search_started", _("image search started")
        IMAGE_SEARCH_COMPLETED = "image_search_completed", _("image search completed")
        IMAGE_SEARCH_FAILED = "image_search_failed", _("image search failed")
        VISUAL_PRODUCT_CLICKED = "visual_product_clicked", _("visual product clicked")
        FIND_SIMILAR_USED = "find_similar_used", _("find similar used")
        # Merchandising & Discovery (Phase 28)
        RECENTLY_VIEWED_CLICK = "recently_viewed_click", _("recently viewed click")
        RECOMMENDATION_CLICK = "recommendation_click", _("recommendation click")
        QUICK_VIEW_OPENED = "quick_view_opened", _("quick view opened")
        WISHLIST_TO_CART = "wishlist_to_cart", _("wishlist to cart")
        CONTINUE_SHOPPING_CLICK = "continue_shopping_click", _("continue shopping click")
        RELATED_PRODUCT_CLICK = "related_product_click", _("related product click")
        # Visual Content & Merchandising Studio (Phase 29)
        HOMEPAGE_SECTION_VIEW = "homepage_section_view", _("homepage section view")
        HOMEPAGE_SECTION_CLICK = "homepage_section_click", _("homepage section click")
        EDITORIAL_VIEW = "editorial_view", _("editorial view")
        EDITORIAL_CLICK = "editorial_click", _("editorial click")
        CAMPAIGN_CLICK = "campaign_click", _("campaign click")
        FEATURED_PRODUCT_CLICK = "featured_product_click", _("featured product click")
        # Conversion, Trust & Purchase Experience (Phase 30)
        PRODUCT_TO_CART = "product_to_cart", _("product to cart")
        CART_VIEW = "cart_view", _("cart view")
        CHECKOUT_STEP_COMPLETED = "checkout_step_completed", _("checkout step completed")
        CHECKOUT_ABANDONED = "checkout_abandoned", _("checkout abandoned")
        PAYMENT_STARTED = "payment_started", _("payment started")
        PAYMENT_FAILED = "payment_failed", _("payment failed")
        ORDER_COMPLETED = "order_completed", _("order completed")
        ALTERNATIVE_PRODUCT_CLICKED = (
            "alternative_product_clicked",
            _("alternative product clicked"),
        )

    name = models.CharField(_("name"), max_length=64, choices=Name.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analytics_events",
        verbose_name=_("user"),
    )
    #: Random first-party id minted by the analytics middleware (no PII; see the app docstring).
    visitor_id = models.CharField(_("visitor id"), max_length=64, blank=True, default="")
    #: The Django session key, when the visitor has one -- the same handle guest carts use.
    session_key = models.CharField(_("session key"), max_length=64, blank=True, default="")
    object_type = models.CharField(_("object type"), max_length=32, blank=True, default="")
    object_id = models.PositiveIntegerField(_("object id"), null=True, blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    #: Non-empty only when the event must not double-count; unique across the table.
    idempotency_key = models.CharField(_("idempotency key"), max_length=200, blank=True, default="")
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("analytics event")
        verbose_name_plural = _("analytics events")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["name", "-created_at"], name="analytics_event_name_created"),
            models.Index(fields=["visitor_id", "-created_at"], name="analytics_event_visitor"),
            models.Index(fields=["object_type", "object_id"], name="analytics_event_object"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="analytics_event_one_per_key",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


class VisitorAttribution(models.Model):
    """First/last-touch attribution for one visitor id.

    First touch wins the ``utm_*`` fields and the landing page; later campaign touches
    refresh ``last_touch_at`` and the utm fields so the most recent campaign is visible
    without building a multi-touch model. Rows appear when a campaign lands or when the
    visitor's first tracked event is recorded -- whichever comes first.
    """

    visitor_id = models.CharField(_("visitor id"), max_length=64, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("user"),
    )
    utm_source = models.CharField(_("utm source"), max_length=120, blank=True, default="")
    utm_medium = models.CharField(_("utm medium"), max_length=120, blank=True, default="")
    utm_campaign = models.CharField(_("utm campaign"), max_length=120, blank=True, default="")
    utm_term = models.CharField(_("utm term"), max_length=120, blank=True, default="")
    utm_content = models.CharField(_("utm content"), max_length=120, blank=True, default="")
    #: External referrer only (same-site referrers are dropped on capture).
    referrer = models.CharField(_("referrer"), max_length=500, blank=True, default="")
    landing_page = models.CharField(_("landing page"), max_length=500, blank=True, default="")
    first_touch_at = models.DateTimeField(_("first touch at"), auto_now_add=True)
    last_touch_at = models.DateTimeField(_("last touch at"), auto_now=True)

    class Meta:
        verbose_name = _("visitor attribution")
        verbose_name_plural = _("visitor attributions")
        ordering = ("-last_touch_at",)
        indexes = [
            models.Index(fields=["user", "-last_touch_at"], name="analytics_attr_user"),
        ]

    def __str__(self) -> str:
        return f"{self.visitor_id} ({self.utm_source or 'direct'})"
