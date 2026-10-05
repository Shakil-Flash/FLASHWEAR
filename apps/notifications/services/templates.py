"""The notification type registry (Phase 17 §4, §10).

Every notification the platform can send is declared here, once: which preference category
owns it, which channels it wants by default, how its title/body render in the notification
center, and which email templates carry it. Domain services pass a *type* and a context;
they never hand-craft copy, which is what keeps voice consistent and makes the preference
system meaningful (a preference can only suppress a channel if the channel choice lives in
one place).

Rendering rules:

* title/body are inline Django templates rendered with autoescaping on -- context values
  from any domain become safe HTML, so an attacker-controlled product name cannot inject
  markup into the notification center;
* ``email_subject`` defaults to the title when empty; ``email_html``/``email_text``
  default to the generic branded pair, so a new type is registrable with two lines and
  still produces a real email;
* context variables are documented per family and rendered defensively (``|default``),
  because a missing optional key must degrade, not 500 a worker.

Channel policy by category:

* **mandatory** -- account, orders, payments, delivery, support: the dispatcher delivers on
  the registry's channels regardless of preferences. These are transactional truths the
  customer must be able to see (spec §14).
* **optional** -- everything else: registry channels are the *request*, the customer's
  ``NotificationPreference`` row decides (and marketing email additionally honours the
  profile opt-in plus the one-click unsubscribe).

A few types deliberately declare a narrower channel set than their category: support email
still flows through the Phase 15 mailer (this system adds the in-app copy, never a second
email), ``account_created`` does not duplicate the verification email, and
``order_confirmed``/``payment_pending`` are in-app acknowledgements rather than an inbox
storm next to the payment email.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from django.template import Context, Template
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _

from apps.notifications.models import (
    Channel,
    NotificationCategory,
    NotificationType,
    Priority,
)

__all__ = [
    "MANDATORY_CATEGORIES",
    "MARKETING_CATEGORIES",
    "TypeSpec",
    "category_defaults",
    "get_spec",
    "registry",
    "render_email",
    "render_notification",
]


@dataclass(frozen=True)
class TypeSpec:
    """Everything the platform knows about one notification type."""

    notification_type: str
    category: str
    channels: tuple[str, ...]
    title: str
    body: str
    email_subject: str = ""
    email_html: str = ""
    email_text: str = ""
    priority: str = Priority.NORMAL

    @property
    def is_mandatory(self) -> bool:
        return self.category in MANDATORY_CATEGORIES

    @property
    def wants_email(self) -> bool:
        return Channel.EMAIL in self.channels

    @property
    def wants_in_app(self) -> bool:
        return Channel.IN_APP in self.channels


_IN_APP_ONLY = (Channel.IN_APP,)
_EMAIL_AND_APP = (Channel.IN_APP, Channel.EMAIL)

#: Categories the customer cannot switch off (Phase 17 §14).
MANDATORY_CATEGORIES = frozenset(
    {
        NotificationCategory.ACCOUNT,
        NotificationCategory.ORDERS,
        NotificationCategory.PAYMENTS,
        NotificationCategory.DELIVERY,
        NotificationCategory.SUPPORT,
    }
)

#: Optional categories whose email leg also requires the marketing opt-in on the profile
#: and is what the one-click unsubscribe token turns off.
MARKETING_CATEGORIES = frozenset({NotificationCategory.PROMOTIONS})


def category_defaults(category: str) -> dict[str, bool]:
    """Out-of-the-box channel switches for a category (used to seed preference rows).

    Mandatory categories default to everything on -- the dispatcher overrides them anyway,
    but the preferences UI shows honest state instead of phantom toggles. Optional
    categories: transactional-but-optional families (loyalty, drops, creator, loop, quests)
    ship with email on; promotions ships with email *off* (marketing must be opted into),
    recommendations ships in-app only (an email digest nobody asked for is spam).
    """
    if category in MANDATORY_CATEGORIES:
        return {"email": True, "in_app": True}
    if category in (NotificationCategory.PROMOTIONS, NotificationCategory.RECOMMENDATIONS):
        return {"email": False, "in_app": True}
    return {"email": True, "in_app": True}


def _spec(
    notification_type: NotificationType,
    category: NotificationCategory,
    channels: tuple[str, ...],
    title: str,
    body: str,
    *,
    email_subject: str = "",
    email_html: str = "",
    email_text: str = "",
    priority: str = Priority.NORMAL,
) -> TypeSpec:
    return TypeSpec(
        notification_type=notification_type,
        category=category,
        channels=channels,
        title=title,
        body=body,
        email_subject=email_subject,
        email_html=email_html,
        email_text=email_text,
        priority=priority,
    )


@lru_cache(maxsize=1)
def registry() -> dict[str, TypeSpec]:
    """The full type -> spec mapping. Immutable after first use."""
    specs = [
        # -- account & security -------------------------------------------------
        _spec(
            NotificationType.ACCOUNT_CREATED,
            NotificationCategory.ACCOUNT,
            _IN_APP_ONLY,  # the verification email already exists; do not double-send
            _("Welcome to FLASHWEAR"),
            _("Your account is ready. Take your style DNA and build your FLASH Closet."),
        ),
        _spec(
            NotificationType.PASSWORD_CHANGED,
            NotificationCategory.ACCOUNT,
            _EMAIL_AND_APP,
            _("Your password was changed"),
            _(
                "If this was not you, reset your password immediately and review your "
                "account security."
            ),
            email_subject=_("Your FLASHWEAR password was changed"),
            email_html="emails/account/password_changed.html",
            email_text="emails/account/password_changed.txt",
        ),
        _spec(
            NotificationType.EMAIL_CHANGED,
            NotificationCategory.ACCOUNT,
            _EMAIL_AND_APP,
            _("Your email address was changed"),
            _("If this was not you, contact the support team immediately."),
        ),
        _spec(
            NotificationType.LOGIN_SECURITY,
            NotificationCategory.ACCOUNT,
            _EMAIL_AND_APP,
            _("New sign-in to your account"),
            _("We noticed a sign-in from a new device or location."),
        ),
        # -- orders -------------------------------------------------------------
        _spec(
            NotificationType.ORDER_PLACED,
            NotificationCategory.ORDERS,
            _EMAIL_AND_APP,
            _('Order {{ order_number|default:"" }} confirmed'),
            _(
                "Thanks for your order{% if total %} of {{ total }}{% endif %}. We are "
                "getting it ready."
            ),
            email_subject=_('Order {{ order_number|default:"" }} confirmed'),
            email_html="emails/orders/order_placed.html",
            email_text="emails/orders/order_placed.txt",
            priority=Priority.HIGH,
        ),
        _spec(
            NotificationType.ORDER_CONFIRMED,
            NotificationCategory.ORDERS,
            _IN_APP_ONLY,  # payment already emails; this is the in-app acknowledgement
            _('Payment confirmed for order {{ order_number|default:"" }}'),
            _("Your payment cleared. The order is now being prepared."),
        ),
        _spec(
            NotificationType.ORDER_PROCESSING,
            NotificationCategory.ORDERS,
            _EMAIL_AND_APP,
            _('Your order {{ order_number|default:"" }} is being prepared'),
            _("We are packing your items and will let you know as soon as they ship."),
            email_html="emails/orders/order_processing.html",
            email_text="emails/orders/order_processing.txt",
        ),
        _spec(
            NotificationType.ORDER_SHIPPED,
            NotificationCategory.ORDERS,
            _EMAIL_AND_APP,
            _('Your order {{ order_number|default:"" }} is on its way'),
            _(
                "It has left our warehouse{% if carrier %} with {{ carrier }}{% endif %}"
                "{% if tracking_number %} (tracking {{ tracking_number }}){% endif %}."
            ),
            email_subject=_('Order {{ order_number|default:"" }} has shipped'),
            email_html="emails/orders/order_shipped.html",
            email_text="emails/orders/order_shipped.txt",
            priority=Priority.HIGH,
        ),
        _spec(
            NotificationType.ORDER_DELIVERED,
            NotificationCategory.ORDERS,
            _EMAIL_AND_APP,
            _('Your order {{ order_number|default:"" }} was delivered'),
            _("Enjoy it. You have 30 days to return anything that is not quite right."),
            email_html="emails/orders/order_delivered.html",
            email_text="emails/orders/order_delivered.txt",
        ),
        _spec(
            NotificationType.ORDER_CANCELLED,
            NotificationCategory.ORDERS,
            _EMAIL_AND_APP,
            _('Your order {{ order_number|default:"" }} was cancelled'),
            _(
                "{% if cancellation_reason %}{{ cancellation_reason }}. {% endif %}"
                "Any payment will be refunded to your original method."
            ),
            email_html="emails/orders/order_cancelled.html",
            email_text="emails/orders/order_cancelled.txt",
        ),
        # -- payments -----------------------------------------------------------
        _spec(
            NotificationType.PAYMENT_PENDING,
            NotificationCategory.PAYMENTS,
            _IN_APP_ONLY,  # a "we are waiting" email with no action is noise
            _('Payment pending for order {{ order_number|default:"" }}'),
            _("We are waiting for your payment provider to confirm the payment."),
        ),
        _spec(
            NotificationType.PAYMENT_SUCCESS,
            NotificationCategory.PAYMENTS,
            _EMAIL_AND_APP,
            _('Payment received for order {{ order_number|default:"" }}'),
            _('We received {{ amount|default:"your payment" }}. Your order is confirmed.'),
            email_subject=_('Payment received - order {{ order_number|default:"" }}'),
            email_html="emails/payments/payment_success.html",
            email_text="emails/payments/payment_success.txt",
            priority=Priority.HIGH,
        ),
        _spec(
            NotificationType.PAYMENT_FAILED,
            NotificationCategory.PAYMENTS,
            _EMAIL_AND_APP,
            _("We could not process your payment"),
            _(
                'Your payment for order {{ order_number|default:"" }} did not go '
                "through{% if payment_error %}: {{ payment_error }}{% endif %}. Please "
                "try again."
            ),
            email_subject=_('Payment failed - order {{ order_number|default:"" }}'),
            email_html="emails/payments/payment_failed.html",
            email_text="emails/payments/payment_failed.txt",
            priority=Priority.HIGH,
        ),
        _spec(
            NotificationType.PAYMENT_REFUNDED,
            NotificationCategory.PAYMENTS,
            _EMAIL_AND_APP,
            _("Your refund is on its way"),
            _(
                'We refunded {{ amount|default:"your payment" }} for order '
                '{{ order_number|default:"" }}.'
            ),
        ),
        # -- delivery / carrier events (reserved: order lifecycle covers the happy path)
        _spec(
            NotificationType.SHIPMENT_CREATED,
            NotificationCategory.DELIVERY,
            _EMAIL_AND_APP,
            _('Shipment created for order {{ order_number|default:"" }}'),
            _("Your parcel is being prepared for dispatch."),
        ),
        _spec(
            NotificationType.SHIPMENT_SHIPPED,
            NotificationCategory.DELIVERY,
            _EMAIL_AND_APP,
            _('Shipment for order {{ order_number|default:"" }} has left'),
            _('Carrier {{ carrier|default:"" }} is transporting your parcel.'),
        ),
        _spec(
            NotificationType.SHIPMENT_DELAYED,
            NotificationCategory.DELIVERY,
            _EMAIL_AND_APP,
            _('Delivery of order {{ order_number|default:"" }} is delayed'),
            _("The carrier reported a delay. We are on it."),
            priority=Priority.HIGH,
        ),
        _spec(
            NotificationType.SHIPMENT_DELIVERED,
            NotificationCategory.DELIVERY,
            _EMAIL_AND_APP,
            _('Order {{ order_number|default:"" }} was delivered'),
            _("Your parcel arrived."),
        ),
        # -- support (in-app copy of messages the Phase 15 mailer already emails) ----
        _spec(
            NotificationType.SUPPORT_TICKET_CREATED,
            NotificationCategory.SUPPORT,
            _IN_APP_ONLY,  # Phase 15 emails the desk; the customer gets the center entry
            _('Ticket {{ ticket_number|default:"" }} opened'),
            _("We received your request and will get back to you here."),
        ),
        _spec(
            NotificationType.SUPPORT_AGENT_REPLY,
            NotificationCategory.SUPPORT,
            _IN_APP_ONLY,  # notify_customer() sends the email; this is the in-app copy
            _('The support team replied on ticket {{ ticket_number|default:"" }}'),
            _("Open the ticket to read the reply and continue the conversation."),
            priority=Priority.HIGH,
        ),
        _spec(
            NotificationType.SUPPORT_TICKET_UPDATED,
            NotificationCategory.SUPPORT,
            _IN_APP_ONLY,
            _('Ticket {{ ticket_number|default:"" }} was updated'),
            _("Open the ticket to see what changed."),
        ),
        _spec(
            NotificationType.SUPPORT_TICKET_RESOLVED,
            NotificationCategory.SUPPORT,
            _IN_APP_ONLY,
            _('Ticket {{ ticket_number|default:"" }} is resolved'),
            _("If anything is still wrong, reopen the ticket and tell us."),
        ),
        # -- loyalty ------------------------------------------------------------
        _spec(
            NotificationType.POINTS_EARNED,
            NotificationCategory.LOYALTY,
            _EMAIL_AND_APP,
            _('You earned {{ points|default:"" }} FLASH Points'),
            _(
                'Order {{ order_number|default:"" }} added points to your balance. '
                'You now have {{ balance|default:"" }}.'
            ),
            email_subject=_("You earned FLASH Points"),
            email_html="emails/loyalty/points_earned.html",
            email_text="emails/loyalty/points_earned.txt",
        ),
        _spec(
            NotificationType.POINTS_REDEEMED,
            NotificationCategory.LOYALTY,
            _EMAIL_AND_APP,
            _('You used {{ points|default:"" }} FLASH Points'),
            _('Your balance is now {{ balance|default:"" }}.'),
        ),
        _spec(
            NotificationType.POINTS_EXPIRING,
            NotificationCategory.LOYALTY,
            _EMAIL_AND_APP,
            _('{{ points|default:"" }} FLASH Points expire soon'),
            _('Use them before {{ expires_on|default:"soon" }} or they will leave your balance.'),
            email_subject=_("Your FLASH Points are expiring"),
        ),
        # -- promotions (email gated by marketing opt-in + unsubscribe) -------------
        _spec(
            NotificationType.PROMOTION_AVAILABLE,
            NotificationCategory.PROMOTIONS,
            _EMAIL_AND_APP,
            _("A new offer for you"),
            _(
                "{% if promotion_name %}{{ promotion_name }}{% else %}A new "
                "promotion{% endif %} is live in the app."
            ),
        ),
        _spec(
            NotificationType.PROMOTION_EXPIRING,
            NotificationCategory.PROMOTIONS,
            _EMAIL_AND_APP,
            _("Your offer ends soon"),
            _(
                "{% if promotion_name %}{{ promotion_name }}{% endif %} expires "
                '{{ expires_on|default:"soon" }}.'
            ),
        ),
        # -- drops --------------------------------------------------------------
        _spec(
            NotificationType.DROP_UPCOMING,
            NotificationCategory.DROPS,
            _IN_APP_ONLY,  # the reminder you asked for by registering interest
            _("{% if drop_name %}{{ drop_name }}{% else %}A FLASH Drop{% endif %} is coming up"),
            _(
                "You asked us to remind you. It opens at "
                '{{ starts_at|default:"the announced time" }}.'
            ),
        ),
        _spec(
            NotificationType.DROP_LIVE,
            NotificationCategory.DROPS,
            _EMAIL_AND_APP,
            _("{% if drop_name %}{{ drop_name }}{% else %}A FLASH Drop{% endif %} is live"),
            _("The drop you asked about is open. Limited pieces, first come first served."),
            email_subject=_("A FLASH Drop you asked about is live"),
            email_html="emails/drops/drop_live.html",
            email_text="emails/drops/drop_live.txt",
            priority=Priority.HIGH,
        ),
        _spec(
            NotificationType.DROP_ENDED,
            NotificationCategory.DROPS,
            _IN_APP_ONLY,
            _("{% if drop_name %}{{ drop_name }}{% else %}The FLASH Drop{% endif %} has ended"),
            _("Thanks for following it. More drops are on the way."),
        ),
        # -- creator ------------------------------------------------------------
        _spec(
            NotificationType.CREATOR_APPLICATION_UPDATED,
            NotificationCategory.CREATOR,
            _EMAIL_AND_APP,
            _("Your creator application was updated"),
            _("Open your creator dashboard to see the outcome."),
        ),
        _spec(
            NotificationType.CREATOR_POST_MODERATION,
            NotificationCategory.CREATOR,
            _EMAIL_AND_APP,
            _("Your post was reviewed"),
            _(
                "{% if moderation_note %}{{ moderation_note }}{% else %}Open your dashboard "
                "for the details.{% endif %}"
            ),
        ),
        _spec(
            NotificationType.CREATOR_POST_FEATURED,
            NotificationCategory.CREATOR,
            _EMAIL_AND_APP,
            _("Your post was featured"),
            _("Nice work -- your post is featured on FLASHWEAR."),
        ),
        # -- FLASH Loop ---------------------------------------------------------
        _spec(
            NotificationType.LOOP_LISTING_APPROVED,
            NotificationCategory.LOOP,
            _EMAIL_AND_APP,
            _("Your listing is live"),
            _(
                "{% if item_name %}{{ item_name }}{% else %}Your item{% endif %} was "
                "approved and is now on the resale shelf."
            ),
            email_subject=_("Your FLASH Loop listing is live"),
        ),
        _spec(
            NotificationType.LOOP_LISTING_REJECTED,
            NotificationCategory.LOOP,
            _EMAIL_AND_APP,
            _("Your listing needs changes"),
            _(
                "{% if rejection_reason %}{{ rejection_reason }}{% else %}Open the item "
                "to see why.{% endif %}"
            ),
        ),
        _spec(
            NotificationType.TRADE_IN_UPDATED,
            NotificationCategory.LOOP,
            _EMAIL_AND_APP,
            _("Trade-in update"),
            _(
                "Your trade-in is now: {% if new_status %}{{ new_status }}{% else %}"
                "updated{% endif %}."
            ),
        ),
        _spec(
            NotificationType.RECYCLING_UPDATED,
            NotificationCategory.LOOP,
            _EMAIL_AND_APP,
            _("Recycling update"),
            _(
                "Your recycling request is now: {% if new_status %}{{ new_status }}"
                "{% else %}updated{% endif %}."
            ),
        ),
        # -- quests -------------------------------------------------------------
        _spec(
            NotificationType.QUEST_COMPLETED,
            NotificationCategory.QUESTS,
            _EMAIL_AND_APP,
            _("Quest complete: {% if quest_name %}{{ quest_name }}{% else %}nice work{% endif %}"),
            _("You finished the quest. Your reward is on its way to your account."),
            email_subject=_("Quest completed"),
        ),
        _spec(
            NotificationType.QUEST_REWARD_GRANTED,
            NotificationCategory.QUESTS,
            _EMAIL_AND_APP,
            _(
                "Reward unlocked: {% if reward_name %}{{ reward_name }}"
                "{% else %}a new reward{% endif %}"
            ),
            _(
                "{% if reward_description %}{{ reward_description }}{% else %}Open the "
                "rewards page to claim it.{% endif %}"
            ),
            email_subject=_("A new reward is waiting"),
        ),
        _spec(
            NotificationType.BADGE_EARNED,
            NotificationCategory.QUESTS,
            _EMAIL_AND_APP,
            _("New badge: {% if badge_name %}{{ badge_name }}{% else %}earned{% endif %}"),
            _("You added a badge to your profile."),
            email_subject=_("You earned a badge"),
        ),
    ]
    return {spec.notification_type: spec for spec in specs}


def get_spec(notification_type: str) -> TypeSpec:
    """Look up a type's spec.

    Raises ``KeyError`` (turnable into a ValueError by the dispatcher): an unknown type is
    a programming error, and silently routing it to a generic spec would hide the bug.
    """
    return registry()[notification_type]


# =============================================================================
# Rendering
# =============================================================================


def _render(template_string: str, context: dict[str, Any]) -> str:
    """Compile-and-render inline template source with autoescaping on.

    Every context value arrives from a domain service (an order number, a product name a
    stranger may have typed) -- autoescaping here is what stops stored XSS travelling
    through a notification title into the customer's center.
    """
    return Template(str(template_string)).render(Context(context))


def render_notification(spec: TypeSpec, context: dict[str, Any]) -> tuple[str, str]:
    """Render the in-app title and body. ``KeyError`` on an unknown type stays loud."""
    title = _render(spec.title, context).strip()
    body = _render(spec.body, context).strip()
    return title[:200], body


def render_email(notification) -> tuple[str, str, str]:
    """Render ``(subject, text, html)`` for a queued email notification.

    Context = the stored, already-safe metadata (this is why metadata is captured at
    dispatch: a re-render weeks later must not need the original domain row) plus the
    presentation keys: absolute links, the recipient's first name, and -- for marketing
    categories -- the one-click unsubscribe URL minted from an opaque token.

    ``TemplateDoesNotExist``/``TemplateSyntaxError`` propagate: a missing template is a
    deploy bug, and the caller maps it to a permanent failure instead of retrying.
    """
    from apps.accounts.mailer import absolute_url, resolve_base_url
    from apps.notifications.services.preferences import ensure_unsubscribe_token

    spec = get_spec(notification.notification_type)
    user = notification.user
    base_url = resolve_base_url(None)
    context: dict[str, Any] = {
        **notification.safe_metadata,
        "recipient_name": (getattr(user, "first_name", "") or "").strip()
        or (getattr(user, "email", "") or "").split("@")[0],
        "site_name": "FLASHWEAR",
        "base_url": base_url,
        "notification_title": _render(spec.title, notification.safe_metadata),
        "notification_body": _render(spec.body, notification.safe_metadata),
    }
    if notification.action_url:
        context["action_url"] = absolute_url(notification.action_url)

    # Footer links. Mandatory categories cannot be unsubscribed from -- offering the
    # button would be a lie -- so they only get the preferences link.
    from django.urls import reverse

    context["preferences_url"] = absolute_url(reverse("account:notifications"))
    if spec.category in MARKETING_CATEGORIES:
        token = ensure_unsubscribe_token(user)
        context["unsubscribe_url"] = absolute_url(
            reverse("notifications:unsubscribe", args=[token.token])
        )

    subject = _render(spec.email_subject or spec.title, context)
    subject = " ".join(subject.split())[:200]  # subjects must stay single-line
    text = render_to_string(spec.email_text or "emails/generic_notification.txt", context).strip()
    html = render_to_string(spec.email_html or "emails/generic_notification.html", context).strip()
    return subject, text, html
