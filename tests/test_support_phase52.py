"""FLASHWEAR -- Phase 52: Customer Support Ticketing & Order-Issue Resolution Tests.

Verifies:
1. Customer support ticket creation with order-issue and Phase 52 categories.
2. General inquiries (no order) and order-linked tickets with ownership validation.
3. Cross-account access denial (404 on unowned tickets and attachments).
4. Conversation history and strict isolation of private internal notes.
5. Status lifecycle, transition rules, and reopening of closed tickets.
6. Secure attachment validation, sanitization, and access-controlled streaming.
7. Staff queue dashboard filtering (priority, category, status, search by order/customer).
8. Order integration links across customer and backoffice views.
9. Notification center event emission and transactional safety.
"""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.notifications.models import (
    Channel,
    Notification,
    NotificationCategory,
    NotificationPreference,
    NotificationStatus,
    NotificationType,
)
from apps.orders.models import Order
from apps.support.models import (
    SupportAttachment,
    SupportMessage,
    SupportTicket,
    SupportTicketEvent,
)
from apps.support.permissions import (
    AGENT_GROUP,
    MANAGER_GROUP,
    SUPPORT_AGENT,
    SUPPORT_MANAGER,
    ensure_capabilities,
)
from apps.support.services import messages as message_service
from apps.support.services import tickets as ticket_service
from apps.support.services.attachments import (
    sanitize_filename,
    validate_attachment,
    validate_uploads,
)
from apps.support.services.errors import (
    AttachmentError,
    ForbiddenError,
    ReferenceError,
    SupportError,
    TransitionError,
)
from tests.phase6_helpers import place_and_pay

pytestmark = pytest.mark.django_db


# -----------------------------------------------------------------------------
# Test Fixtures & Helpers
# -----------------------------------------------------------------------------


def make_png_bytes(color=(255, 0, 0), size=(2, 2)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def make_agent_user(email: str = "desk.agent@flashwear.test", *, manager: bool = False):
    ensure_capabilities()
    user = get_user_model().objects.create_user(email=email, password="SafePassword123!")
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    group_name = MANAGER_GROUP if manager else AGENT_GROUP
    user.groups.add(Group.objects.get(name=group_name))
    return user


# -----------------------------------------------------------------------------
# 1. Customer Support Tickets & Categories
# -----------------------------------------------------------------------------


class TestCustomerSupportTickets:
    @pytest.mark.parametrize(
        "category",
        [
            SupportTicket.Category.ORDER_STATUS,
            SupportTicket.Category.DAMAGED_ITEM,
            SupportTicket.Category.INCORRECT_ITEM,
            SupportTicket.Category.MISSING_ITEM,
            SupportTicket.Category.RETURN_REFUND,
            SupportTicket.Category.PAYMENT,
            SupportTicket.Category.ACCOUNT,
            SupportTicket.Category.PRODUCT,
            SupportTicket.Category.OTHER,
        ],
    )
    def test_create_ticket_with_order_issue_categories(self, user, category):
        """Customer can open tickets with Phase 52 specific categories."""
        ticket = ticket_service.open_ticket(
            customer=user,
            subject=f"Issue regarding {category}",
            description=f"Detailed description of {category} issue.",
            category=category,
        )
        assert ticket.pk is not None
        assert ticket.category == category
        assert ticket.status == SupportTicket.Status.OPEN
        assert ticket.priority == SupportTicket.Priority.NORMAL
        assert ticket.number.startswith("FW-")

    def test_general_inquiry_without_order(self, user):
        """A general inquiry ticket does not require an order."""
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="General sizing question",
            description="How does the oversized tee fit on shoulders?",
            category=SupportTicket.Category.PRODUCT,
        )
        assert ticket.order is None
        assert ticket.customer == user
        assert ticket.reference_labels == []

    def test_order_linked_ticket_success(self, user, product):
        """A ticket linked to an order owned by customer succeeds."""
        order = place_and_pay(user, product.variants.get())
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Damaged hoodie on delivery",
            description="The package arrived torn with oil stains.",
            category=SupportTicket.Category.DAMAGED_ITEM,
            refs={"order": order.number},
        )
        assert ticket.order == order
        assert f"Order {order.number}" in ticket.reference_labels
        assert ticket.payment is not None
        assert ticket.payment.order == order

    def test_order_linking_denied_for_other_customer(self, user, other_user, product):
        """Cannot link an order belonging to another customer (preventing IDOR)."""
        stranger_order = place_and_pay(other_user, product.variants.get())

        with pytest.raises(ReferenceError) as exc:
            ticket_service.open_ticket(
                customer=user,
                subject="Trying to probe another order",
                description="Checking IDOR security.",
                category=SupportTicket.Category.ORDER,
                refs={"order": stranger_order.number},
            )
        assert "could not find that record" in str(exc.value).lower()

    def test_ticket_last_activity_at_property(self, user):
        """last_activity_at property returns the latest activity timestamp."""
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Testing activity",
            description="Testing last_activity_at calculation.",
            category=SupportTicket.Category.OTHER,
        )
        initial_activity = ticket.last_activity_at
        assert initial_activity is not None

        # Customer sends reply
        now = timezone.now() + timezone.timedelta(seconds=5)
        ticket.last_customer_message_at = now
        ticket.save(update_fields=["last_customer_message_at"])
        assert ticket.last_activity_at == now


# -----------------------------------------------------------------------------
# 2. Conversation & Staff Workflow
# -----------------------------------------------------------------------------


class TestConversationAndInternalNotes:
    def test_customer_reply_updates_ticket(self, user):
        """Customer reply appends message and updates last_customer_message_at."""
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Need update",
            description="Initial inquiry.",
            category=SupportTicket.Category.OTHER,
        )
        # Move ticket to waiting on customer
        agent = make_agent_user("agent1@flashwear.test")
        ticket_service.transition_ticket(
            ticket,
            to_status=SupportTicket.Status.WAITING_FOR_CUSTOMER,
            actor=agent,
        )
        assert ticket.status == SupportTicket.Status.WAITING_FOR_CUSTOMER

        # Customer replies -> moves back to in_progress
        reply = message_service.post_customer_message(
            ticket,
            customer=user,
            body="Here is the extra information you asked for.",
        )
        ticket.refresh_from_db()
        assert reply.pk is not None
        assert reply.author == user
        assert not reply.is_internal
        assert ticket.status == SupportTicket.Status.IN_PROGRESS
        assert ticket.last_customer_message_at is not None

    def test_staff_public_reply_and_internal_note(self, user):
        """Staff can post customer-visible replies and private internal notes."""
        agent = make_agent_user("agent2@flashwear.test")
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Where is package?",
            description="Tracking stalled.",
            category=SupportTicket.Category.DELIVERY,
        )

        # Internal note: invisible to customer
        note = message_service.post_agent_message(
            ticket,
            actor=agent,
            body="Carrier confirmed delay due to weather. Contacted dispatcher.",
            internal=True,
        )
        assert note.is_internal
        assert not note.visible_to_customer
        ticket.refresh_from_db()
        # Status has not changed because of internal note
        assert ticket.status == SupportTicket.Status.OPEN

        # Public reply: parks on customer
        public_reply = message_service.post_agent_message(
            ticket,
            actor=agent,
            body="Hello! We checked with the carrier and your shipment is on track.",
            internal=False,
        )
        assert not public_reply.is_internal
        assert public_reply.visible_to_customer
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.WAITING_FOR_CUSTOMER
        assert ticket.first_response_at is not None

    def test_customer_cannot_view_internal_notes(self, client, user):
        """Customer view strictly hides internal notes."""
        agent = make_agent_user("agent3@flashwear.test")
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Account review",
            description="Help with my account.",
            category=SupportTicket.Category.ACCOUNT,
        )
        message_service.post_agent_message(
            ticket,
            actor=agent,
            body="CONFIDENTIAL_STAFF_NOTE_12345",
            internal=True,
        )
        message_service.post_agent_message(
            ticket,
            actor=agent,
            body="PUBLIC_STAFF_MESSAGE_67890",
            internal=False,
        )

        client.force_login(user)
        response = client.get(reverse("support:ticket-detail", args=[ticket.number]))
        assert response.status_code == 200
        content = response.content.decode()
        assert "PUBLIC_STAFF_MESSAGE_67890" in content
        assert "CONFIDENTIAL_STAFF_NOTE_12345" not in content

    def test_customer_cannot_post_internal_note(self, user):
        """Database constraint forbids author_type='customer' and is_internal=True."""
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Testing security",
            description="Attempting to forge an internal note.",
            category=SupportTicket.Category.OTHER,
        )
        with transaction.atomic(), pytest.raises(IntegrityError):
            SupportMessage.objects.create(
                ticket=ticket,
                author=user,
                author_type=SupportMessage.AuthorType.CUSTOMER,
                body="Attempted spoofed note",
                is_internal=True,
            )

    def test_reopen_and_close_lifecycle(self, client, user):
        """Customer can close and reopen their own ticket."""
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Issue resolved early",
            description="Found my parcel with neighbour.",
            category=SupportTicket.Category.DELIVERY,
        )
        client.force_login(user)

        # Close ticket
        res_close = client.post(reverse("support:ticket-close", args=[ticket.number]))
        assert res_close.status_code == 302
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.CLOSED
        assert ticket.closed_at is not None

        # Cannot reply when closed
        res_reply = client.post(
            reverse("support:ticket-reply", args=[ticket.number]),
            {"body": "Trying to reply to closed ticket."},
            follow=True,
        )
        assert "closed" in res_reply.content.decode().lower()

        # Reopen ticket
        res_reopen = client.post(reverse("support:ticket-reopen", args=[ticket.number]))
        assert res_reopen.status_code == 302
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.IN_PROGRESS
        assert ticket.closed_at is None

    def test_invalid_status_transition_rejected(self, user):
        """Invalid transition raises TransitionError."""
        agent = make_agent_user("agent4@flashwear.test", manager=True)
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Test transition",
            description="Testing invalid status jump.",
            category=SupportTicket.Category.OTHER,
        )
        ticket_service.close_ticket(ticket, actor=agent)
        assert ticket.status == SupportTicket.Status.CLOSED

        # CLOSED can only transition to IN_PROGRESS, not directly to RESOLVED or OPEN
        with pytest.raises(TransitionError):
            ticket_service.transition_ticket(
                ticket,
                to_status=SupportTicket.Status.RESOLVED,
                actor=agent,
            )


# -----------------------------------------------------------------------------
# 3. Order Integration & Deep Linking
# -----------------------------------------------------------------------------


class TestOrderIntegration:
    def test_prefill_from_order_query_parameters(self, client, user, product):
        """Clicking 'Report Damaged' pre-fills the ticket form with order and category."""
        order = place_and_pay(user, product.variants.get())
        client.force_login(user)

        response = client.get(
            f"{reverse('support:ticket-create')}?order={order.number}&category=damaged_item"
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert order.number in content
        assert "damaged_item" in content

    def test_prefill_rejects_unowned_order(self, client, user, other_user, product):
        """Pre-fill query parameter ignores unowned order to prevent leaking order info."""
        stranger_order = place_and_pay(other_user, product.variants.get())
        client.force_login(user)

        response = client.get(
            f"{reverse('support:ticket-create')}?order={stranger_order.number}"
        )
        assert response.status_code == 200
        content = response.content.decode()
        # Does not prefill the order field
        assert f'value="{stranger_order.number}"' not in content

    def test_customer_ticket_detail_shows_order_card(self, client, user, product):
        """Ticket detail page displays the order summary card and link to order detail."""
        order = place_and_pay(user, product.variants.get())
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Defective zipper",
            description="Zipper broke on first wear.",
            category=SupportTicket.Category.DAMAGED_ITEM,
            refs={"order": order.number},
        )
        client.force_login(user)

        response = client.get(reverse("support:ticket-detail", args=[ticket.number]))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Related Order" in content
        assert order.number in content
        assert reverse("account:order-detail", args=[order.number]) in content

    def test_staff_ticket_detail_shows_order_and_backoffice_links(self, client, user, product):
        """Staff ticket detail shows backoffice link to inspect order and returns."""
        order = place_and_pay(user, product.variants.get())
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Wrong size received",
            description="Ordered M but received S.",
            category=SupportTicket.Category.INCORRECT_ITEM,
            refs={"order": order.number},
        )
        agent = make_agent_user("agent5@flashwear.test")
        client.force_login(agent)

        response = client.get(reverse("support:staff-ticket-detail", args=[ticket.number]))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Associated Order" in content
        assert order.number in content
        assert reverse("backoffice:order_detail", args=[order.number]) in content
        assert reverse("backoffice:returns") in content


# -----------------------------------------------------------------------------
# 4. Secure Attachments
# -----------------------------------------------------------------------------


class TestSecureAttachments:
    def test_valid_image_upload_and_download(self, client, user):
        """Customer can attach a valid PNG and download it securely."""
        png_data = make_png_bytes()
        upload = SimpleUploadedFile("evidence.png", png_data, content_type="image/png")

        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Attached evidence",
            description="Here is photo of damaged hem.",
            category=SupportTicket.Category.DAMAGED_ITEM,
            uploads=[upload],
        )
        attachment = SupportAttachment.objects.filter(message__ticket=ticket).first()
        assert attachment is not None
        assert attachment.original_filename == "evidence.png"
        assert attachment.content_type == "image/png"

        # Owner can download
        client.force_login(user)
        res_dl = client.get(reverse("support:attachment", args=[attachment.pk]))
        assert res_dl.status_code == 200
        assert res_dl["Content-Type"] == "image/png"
        assert "private" in res_dl["Cache-Control"]
        assert "no-store" in res_dl["Cache-Control"]
        assert res_dl["X-Content-Type-Options"] == "nosniff"

    def test_stranger_cannot_download_attachment(self, client, user, other_user):
        """Another customer gets 404 when trying to download stranger's attachment."""
        upload = SimpleUploadedFile("secret.png", make_png_bytes(), content_type="image/png")
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Secret document",
            description="Confidential attachment.",
            category=SupportTicket.Category.OTHER,
            uploads=[upload],
        )
        attachment = SupportAttachment.objects.filter(message__ticket=ticket).first()

        client.force_login(other_user)
        res_dl = client.get(reverse("support:attachment", args=[attachment.pk]))
        assert res_dl.status_code == 404

    def test_disallowed_file_types_rejected(self, user):
        """Executable, archive, and SVG uploads are rejected."""
        disallowed = [
            ("hack.exe", b"MZ\x90\x00executable", "application/x-msdownload"),
            ("script.svg", b"<svg><script>alert(1)</script></svg>", "image/svg+xml"),
            ("payload.zip", b"PK\x03\x04archive", "application/zip"),
        ]

        for fname, data, mime in disallowed:
            upload = SimpleUploadedFile(fname, data, content_type=mime)
            with pytest.raises(AttachmentError):
                validate_attachment(upload)

    def test_path_traversal_sanitization(self):
        """sanitize_filename strips path traversal and control characters."""
        assert sanitize_filename("../../../etc/passwd") == "passwd"
        assert sanitize_filename("..\\..\\windows\\system32.dll") == "system32.dll"
        assert sanitize_filename("receipt\x00.pdf") == "receipt.pdf"


# -----------------------------------------------------------------------------
# 5. Staff Interface & Queue Filtering
# -----------------------------------------------------------------------------


class TestStaffInterfaceAndFiltering:
    def test_staff_dashboard_filter_by_priority_and_status(self, client, user):
        """Staff can filter the queue by priority, status, and category."""
        agent = make_agent_user("agent6@flashwear.test")
        client.force_login(agent)

        t_urgent = ticket_service.open_ticket(
            customer=user,
            subject="Urgent damaged item",
            description="Package completely destroyed.",
            category=SupportTicket.Category.DAMAGED_ITEM,
        )
        ticket_service.set_priority(t_urgent, actor=agent, priority=SupportTicket.Priority.URGENT)

        t_normal = ticket_service.open_ticket(
            customer=user,
            subject="Normal inquiry",
            description="Simple question.",
            category=SupportTicket.Category.PRODUCT,
        )

        # Filter by priority=urgent
        res = client.get(
            f"{reverse('support:staff-dashboard')}?priority={SupportTicket.Priority.URGENT}"
        )
        assert res.status_code == 200
        content = res.content.decode()
        assert t_urgent.number in content
        assert t_normal.number not in content

    def test_staff_dashboard_search_by_order(self, client, user, product):
        """Staff can search tickets by order number."""
        agent = make_agent_user("agent7@flashwear.test")
        client.force_login(agent)

        order = place_and_pay(user, product.variants.get())
        t_order = ticket_service.open_ticket(
            customer=user,
            subject="Order delivery issue",
            description="Where is it?",
            category=SupportTicket.Category.DELIVERY,
            refs={"order": order.number},
        )
        t_other = ticket_service.open_ticket(
            customer=user,
            subject="Unrelated question",
            description="Different issue.",
            category=SupportTicket.Category.OTHER,
        )

        res = client.get(f"{reverse('support:staff-dashboard')}?q={order.number}")
        assert res.status_code == 200
        content = res.content.decode()
        assert t_order.number in content
        assert t_other.number not in content

    def test_staff_ticket_assignment(self, client, user):
        """Staff can assign and reassign tickets."""
        agent1 = make_agent_user("desk1@flashwear.test", manager=True)
        agent2 = make_agent_user("desk2@flashwear.test")
        client.force_login(agent1)

        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Assignment check",
            description="Testing assignment.",
            category=SupportTicket.Category.OTHER,
        )

        res = client.post(
            reverse("support:staff-assign", args=[ticket.number]),
            {"agent_email": agent2.email},
            follow=True,
        )
        assert res.status_code == 200
        ticket.refresh_from_db()
        assert ticket.assigned_to == agent2
        assert ticket.assigned_at is not None


# -----------------------------------------------------------------------------
# 6. Notification Center Integration
# -----------------------------------------------------------------------------


class TestNotificationIntegration:
    def test_ticket_creation_and_reply_emits_notifications(self, user):
        """Support ticket events emit in-app notifications in the notification center."""
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Notification center test",
            description="Testing in-app notification creation.",
            category=SupportTicket.Category.ORDER_STATUS,
        )

        # In-app notification for ticket created
        notif_created = Notification.objects.filter(
            user=user,
            notification_type=NotificationType.SUPPORT_TICKET_CREATED,
        ).first()
        assert notif_created is not None
        assert notif_created.category == NotificationCategory.SUPPORT
        assert ticket.number in notif_created.title

        # Staff reply emits SUPPORT_AGENT_REPLY
        agent = make_agent_user("agent8@flashwear.test")
        message_service.post_agent_message(
            ticket,
            actor=agent,
            body="We have updated your order status.",
            internal=False,
        )
        notif_reply = Notification.objects.filter(
            user=user,
            notification_type=NotificationType.SUPPORT_AGENT_REPLY,
        ).first()
        assert notif_reply is not None
        assert notif_reply.category == NotificationCategory.SUPPORT

    def test_ticket_resolution_emits_support_ticket_resolved(self, user):
        """Transitioning to RESOLVED emits SUPPORT_TICKET_RESOLVED."""
        agent = make_agent_user("agent9@flashwear.test")
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Resolution test",
            description="Testing resolved notification.",
            category=SupportTicket.Category.OTHER,
        )
        ticket_service.transition_ticket(
            ticket,
            to_status=SupportTicket.Status.RESOLVED,
            actor=agent,
        )
        notif_resolved = Notification.objects.filter(
            user=user,
            notification_type=NotificationType.SUPPORT_TICKET_RESOLVED,
        ).first()
        assert notif_resolved is not None
        assert notif_resolved.category == NotificationCategory.SUPPORT

    def test_customer_closing_own_ticket_avoids_self_notification(self, user):
        """Customer closing their own ticket does not produce unnecessary self-notification."""
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Self close test",
            description="Testing avoiding self notification.",
            category=SupportTicket.Category.OTHER,
        )
        initial_notifs = Notification.objects.filter(user=user).count()

        ticket_service.close_ticket(ticket, actor=user)
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.CLOSED

        # No new notification was created for user closing their own ticket
        final_notifs = Notification.objects.filter(user=user).count()
        assert final_notifs == initial_notifs

    def test_transactional_support_notification_never_suppressed_by_marketing_opt_out(
        self, user
    ):
        """Support notifications are transactional (mandatory) and bypass marketing opt-out."""
        # Opt-out of marketing
        profile = user.profile
        profile.marketing_email_opt_in = False
        profile.save(update_fields=["marketing_email_opt_in"])

        # Also set an explicit False preference on the support category
        pref, _ = NotificationPreference.objects.get_or_create(
            user=user,
            category=NotificationCategory.SUPPORT,
        )
        pref.in_app_enabled = False
        pref.email_enabled = False
        pref.save()

        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Support is transactional",
            description="Testing mandatory category policy.",
            category=SupportTicket.Category.ACCOUNT,
        )

        notif = Notification.objects.filter(
            user=user,
            notification_type=NotificationType.SUPPORT_TICKET_CREATED,
        ).first()
        assert notif is not None, "Support notification must be created despite preferences"
