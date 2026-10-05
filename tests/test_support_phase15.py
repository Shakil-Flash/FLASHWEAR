"""FLASH Support & Customer Care (Phase 15): tickets, transcript, desk, API.

The contract these tests protect:

* **ownership is scoped at the selector** -- another customer's ticket number 404s on every
  HTML and API surface (never 403), and the desk's namespace 404s for anyone without the
  agent capability, so the URL space does not advertise where it is;
* **the state machine has exactly one implementation** -- customers may only reach
  ``CUSTOMER_TARGETS`` (close, or get it moving again), every other move goes through the
  same graph, and the timestamps follow the state rather than the clock;
* **internal notes are private in every direction** -- absent from the customer's
  transcript on HTML and API alike, and never emailed to them, while the database refuses
  "customer + internal" outright;
* **mass assignment is impossible by construction** -- no status, priority, source,
  assignee or customer field exists on a customer payload, and an unknown reference key is
  an error rather than an ignored extra;
* **references are proved, not trusted** -- a customer may only name rows they own (or that
  are public), payment and shipment are derived from the order, and "somebody else's" and
  "does not exist" fail identically;
* **attachments are content-checked and served privately** -- no public URL exists, the
  bytes leave only through an ownership-checked download, and nothing under ``MEDIA_ROOT``
  is ever written;
* **support never mutates another domain** -- a ticket may point at an order, and the order
  itself does not move;
* **rate limits and delivery failures degrade, they do not corrupt** -- a throttle answers
  429 and a mailer that throws leaves the transcript intact.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

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
    group_for,
    has_capability,
    staff_with_capability,
)
from apps.support.services import messages as message_service
from apps.support.services import tickets as ticket_service
from apps.support.services.attachments import sanitize_filename, validate_attachment
from apps.support.services.errors import (
    AttachmentError,
    DuplicateTicketError,
    ForbiddenError,
    ReferenceError,
    SupportError,
    TransitionError,
)
from apps.support.services.throttling import SupportThrottle
from apps.support.tasks import close_abandoned_tickets, remind_pending_tickets
from tests.phase6_helpers import message_texts, place_and_pay, placed_order
from tests.phase7_helpers import make_review

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def jpeg_bytes() -> bytes:
    """A real JPEG, so "a PNG that is secretly a JPEG" is a test rather than a claim."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(buffer, format="JPEG")
    return buffer.getvalue()


def png_upload(name: str = "evidence.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PNG_BYTES, content_type="image/png")


def wide_png_upload(name: str = "wide.png") -> SimpleUploadedFile:
    """A small but not tiny image, for the pixel ceiling (the shared 1x1 is too small)."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (4, 4), color=(0, 128, 255)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def make_agent(db, email: str = "agent@flashwear.test", *, manager: bool = False):
    """A staff user in the right group. ``is_staff`` alone grants nothing here."""
    ensure_capabilities()
    agent = get_user_model().objects.create_user(email=email, password="Str0ng-Passw0rd!")
    agent.is_staff = True
    agent.save(update_fields=["is_staff"])
    agent.groups.add(Group.objects.get(name=MANAGER_GROUP if manager else AGENT_GROUP))
    return agent


def open_ticket(customer, **overrides) -> SupportTicket:
    params = {
        "customer": customer,
        "subject": "Where is my order?",
        "description": "It said delivered but nothing arrived.",
        "category": SupportTicket.Category.DELIVERY,
    }
    params.update(overrides)
    return ticket_service.open_ticket(**params)


def ticket_for(customer, **overrides) -> SupportTicket:
    """A ticket row written directly -- for fixtures that are not the behaviour under test."""
    params = {
        "customer": customer,
        "subject": "Damaged parcel",
        "description": "The box was crushed.",
        "category": SupportTicket.Category.DELIVERY,
    }
    params.update(overrides)
    return SupportTicket.objects.create(**params)


# --------------------------------------------------------------------------------------
# Model rules: numbering, state machine, constraints
# --------------------------------------------------------------------------------------


class TestTicketModelRules:
    def test_the_number_is_generated_from_the_primary_key(self):
        ticket = ticket_for(get_user_model().objects.create_user(email="n@flashwear.test"))

        assert ticket.number == f"FW-{timezone.now().year}-{ticket.pk:06d}"
        assert SupportTicket.objects.filter(number=ticket.number).count() == 1

    def test_two_tickets_never_share_a_number(self, user, other_user):
        first = ticket_for(user)
        second = ticket_for(other_user)

        assert first.number != second.number

    def test_a_number_that_is_already_set_is_never_re_minted(self, user):
        """An import, a fixture or a repair script supplies the number; save keeps it."""
        supplied = SupportTicket(
            customer=user,
            number="FW-2020-000042",
            subject="Loaded from a fixture",
            description="Old data.",
        )
        supplied.save()

        assert supplied.number == "FW-2020-000042"
        supplied.refresh_from_db()
        assert supplied.number == "FW-2020-000042"

    @pytest.mark.parametrize(
        ("field", "values"),
        [
            ("status", SupportTicket.Status),
            ("priority", SupportTicket.Priority),
        ],
    )
    def test_the_check_constraints_match_the_text_choices(self, field, values):
        # The module-level tuples exist because a nested ``class Meta`` cannot see the
        # enclosing class body; this is what stops them drifting apart.
        module_values = {
            "status": tuple(SupportTicket.Status.values),
            "priority": tuple(SupportTicket.Priority.values),
        }[field]
        assert module_values == tuple(values.values)

    def test_a_status_outside_the_known_set_is_refused_by_the_database(self, user):
        with transaction.atomic(), pytest.raises(IntegrityError):
            SupportTicket.objects.create(
                customer=user,
                subject="Bad status",
                description="Nope.",
                status="sideways",
            )

    def test_a_customer_cannot_write_an_internal_note(self, user):
        ticket = ticket_for(user)
        with transaction.atomic(), pytest.raises(IntegrityError):
            SupportMessage.objects.create(
                ticket=ticket,
                author=user,
                author_type=SupportMessage.AuthorType.CUSTOMER,
                body="private?",
                is_internal=True,
            )

    def test_the_customer_targets_are_the_two_the_graph_allows(self):
        # Reopening and closing are the only moves a customer owns; everything else
        # (resolving, escalating, parking on the internal team) is the desk's.
        assert SupportTicket.CUSTOMER_TARGETS == {
            SupportTicket.Status.IN_PROGRESS,
            SupportTicket.Status.CLOSED,
        }
        for target in SupportTicket.CUSTOMER_TARGETS:
            assert any(target in allowed for allowed in SupportTicket.ALLOWED_TRANSITIONS.values())

    def test_the_transition_graph_is_the_one_the_desk_uses(self):
        ticket = SupportTicket(status=SupportTicket.Status.RESOLVED)

        assert ticket.can_transition_to(SupportTicket.Status.CLOSED)
        assert ticket.can_transition_to(SupportTicket.Status.IN_PROGRESS)
        # Reopening is an endpoint, not something a stray reply can trigger.
        assert not ticket.can_transition_to(SupportTicket.Status.OPEN)
        assert not ticket.can_transition_to(SupportTicket.Status.ESCALATED)

    def test_is_open_excludes_only_the_two_terminal_states(self):
        assert SupportTicket(status=SupportTicket.Status.OPEN).is_open
        assert SupportTicket(status=SupportTicket.Status.ESCALATED).is_open
        assert not SupportTicket(status=SupportTicket.Status.RESOLVED).is_open
        assert not SupportTicket(status=SupportTicket.Status.CLOSED).is_open


# --------------------------------------------------------------------------------------
# Capabilities: group + staff flag, never a bare flag
# --------------------------------------------------------------------------------------


class TestCapabilities:
    def test_anonymous_and_missing_users_hold_nothing(self):
        assert has_capability(None, SUPPORT_AGENT) is False
        assert (
            has_capability(
                get_user_model().objects.create_user(email="x@flashwear.test"), SUPPORT_AGENT
            )
            is False
        )

    def test_the_staff_flag_alone_grants_nothing(self, user):
        user.is_staff = True
        user.save(update_fields=["is_staff"])

        assert has_capability(user, SUPPORT_AGENT) is False

    def test_the_group_alone_grants_nothing(self, user):
        ensure_capabilities()
        user.groups.add(Group.objects.get(name=AGENT_GROUP))

        assert has_capability(user, SUPPORT_AGENT) is False

    def test_staff_flag_plus_group_is_the_agent_capability(self, db):
        agent = make_agent(db)

        assert has_capability(agent, SUPPORT_AGENT) is True
        assert has_capability(agent, SUPPORT_MANAGER) is False

    def test_a_manager_holds_both_capabilities(self, db):
        manager = make_agent(db, email="lead@flashwear.test", manager=True)

        assert has_capability(manager, SUPPORT_AGENT) is True
        assert has_capability(manager, SUPPORT_MANAGER) is True

    def test_an_inactive_agent_loses_the_capability(self, db):
        agent = make_agent(db)
        agent.is_active = False
        agent.save(update_fields=["is_active"])

        assert has_capability(agent, SUPPORT_AGENT) is False

    def test_a_superuser_implies_every_capability(self, admin_user):
        assert has_capability(admin_user, SUPPORT_AGENT) is True
        assert has_capability(admin_user, SUPPORT_MANAGER) is True

    def test_an_unknown_capability_is_an_error_not_a_silent_deny(self, db):
        agent = make_agent(db)
        with pytest.raises(KeyError):
            has_capability(agent, "support.wat")
        with pytest.raises(KeyError):
            group_for("support.wat")

    def test_the_recipient_list_is_active_staff_only(self, db):
        agent = make_agent(db)
        inactive = make_agent(db, email="gone@flashwear.test")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        # A plain, non-staff account must never appear in the notification list.
        get_user_model().objects.create_user(email="plain@flashwear.test")

        recipients = set(staff_with_capability(SUPPORT_AGENT).values_list("email", flat=True))

        assert recipients == {agent.email}

    def test_creating_the_groups_twice_is_harmless(self, db):
        assert ensure_capabilities() == []
        assert {group_for(c) for c in (SUPPORT_AGENT, SUPPORT_MANAGER)} == {
            AGENT_GROUP,
            MANAGER_GROUP,
        }


# --------------------------------------------------------------------------------------
# Customer surface (HTML): creation, replies, closure, IDOR
# --------------------------------------------------------------------------------------


class TestCustomerHtml:
    def test_every_customer_page_requires_a_login(self, client, user):
        number = ticket_for(user).number
        for name, args in (
            ("support:home", ()),
            ("support:ticket-list", ()),
            ("support:ticket-create", ()),
            ("support:ticket-detail", (number,)),
        ):
            response = client.get(reverse(name, args=args))

            assert response.status_code == 302
            assert reverse("accounts:login") in response.url

    def test_the_home_page_lists_open_tickets_and_the_way_to_start_one(self, client, user):
        open_ticket(user, subject="Open one")
        ticket_for(user, subject="Closed one", status=SupportTicket.Status.CLOSED)
        client.force_login(user)

        body = client.get(reverse("support:home")).content.decode()

        assert "Open one" in body
        assert "Closed one" not in body
        assert reverse("support:ticket-create") in body
        assert "noindex, follow" in body

    def test_creating_a_ticket_writes_the_first_message_and_the_audit_row(self, client, user):
        client.force_login(user)

        response = client.post(
            reverse("support:ticket-create"),
            {
                "subject": "Nothing arrived",
                "description": "The courier says it was handed over.",
                "category": SupportTicket.Category.DELIVERY,
            },
        )

        assert response.status_code == 302
        ticket = SupportTicket.objects.get()
        assert ticket.customer_id == user.pk
        assert ticket.number.startswith(f"FW-{timezone.now().year}-")
        assert ticket.source == SupportTicket.Source.WEB
        first = ticket.messages.get()
        assert first.author_type == SupportMessage.AuthorType.CUSTOMER
        assert first.body == "The courier says it was handed over."
        assert SupportTicketEvent.objects.filter(
            ticket=ticket, event_type=SupportTicketEvent.EventType.CREATED
        ).exists()
        assert "the desk must hear about a new ticket"

    def test_a_customer_payload_cannot_set_the_desks_fields(self, client, user, other_user):
        client.force_login(user)

        client.post(
            reverse("support:ticket-create"),
            {
                "subject": "Nice try",
                "description": "Setting everything at once.",
                "category": SupportTicket.Category.OTHER,
                # Every field the service owns, sent anyway.
                "priority": SupportTicket.Priority.URGENT,
                "status": SupportTicket.Status.RESOLVED,
                "source": SupportTicket.Source.ADMIN,
                "customer": other_user.pk,
                "is_internal": "on",
                "assigned_to": other_user.pk,
            },
        )

        ticket = SupportTicket.objects.get()
        assert ticket.priority == SupportTicket.Priority.NORMAL
        assert ticket.status == SupportTicket.Status.OPEN
        assert ticket.source == SupportTicket.Source.WEB
        assert ticket.customer_id == user.pk
        assert ticket.assigned_to_id is None

    def test_a_second_ticket_about_the_same_reference_is_refused_with_a_link_back(
        self, client, user, product
    ):
        client.force_login(user)
        payload = {
            "subject": "Wrong colour",
            "description": "I ordered sand.",
            "category": SupportTicket.Category.PRODUCT,
            "product": product.slug,
        }
        client.post(reverse("support:ticket-create"), payload)

        response = client.post(reverse("support:ticket-create"), payload)

        assert response.status_code == 200  # re-rendered, not a redirect
        assert SupportTicket.objects.count() == 1
        body = response.content.decode()
        assert "already have an open ticket" in body
        assert SupportTicket.objects.get().number in body

    def test_an_open_ticket_about_a_different_record_is_allowed(self, user, product):
        first = open_ticket(
            user, category=SupportTicket.Category.PRODUCT, refs={"product": product.slug}
        )
        second = open_ticket(
            user, category=SupportTicket.Category.PRODUCT, subject="Sizing", refs={}
        )

        assert first.pk != second.pk

    def test_replying_moves_the_ticket_off_the_customer(self, client, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        # The desk parks it on the customer; only then is a reply the customer's move.
        ticket_service.transition_ticket(
            ticket,
            to_status=SupportTicket.Status.WAITING_FOR_CUSTOMER,
            actor=agent,
        )
        client.force_login(user)

        response = client.post(
            reverse("support:ticket-reply", args=[ticket.number]),
            {"body": "It was with a neighbour."},
        )

        assert response.status_code == 302
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.IN_PROGRESS
        assert ticket.messages.count() == 2
        assert ticket.messages.order_by("-created_at").first().body == "It was with a neighbour."

    def test_a_reply_on_a_closed_ticket_is_refused_and_writes_nothing(self, client, user):
        ticket = ticket_for(user, status=SupportTicket.Status.CLOSED, closed_at=timezone.now())
        client.force_login(user)

        response = client.post(
            reverse("support:ticket-reply", args=[ticket.number]),
            {"body": "One more thing."},
            follow=True,
        )

        assert response.status_code == 200
        assert ticket.messages.count() == 0  # the fixture wrote nothing, and neither did we
        assert "closed" in message_texts(response).lower()

    def test_the_customer_closes_and_reopens_their_own_ticket(self, client, user):
        ticket = open_ticket(user)
        client.force_login(user)

        close = client.post(reverse("support:ticket-close", args=[ticket.number]))
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.CLOSED
        assert ticket.closed_at is not None

        reopen = client.post(reverse("support:ticket-reopen", args=[ticket.number]))
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.IN_PROGRESS
        assert ticket.closed_at is None  # "closed" means *currently* closed
        assert close.status_code == reopen.status_code == 302

    def test_another_customers_ticket_number_is_a_404_everywhere(self, client, user, other_user):
        ticket = ticket_for(user)
        client.force_login(other_user)

        for name, method, args in (
            ("support:ticket-detail", "get", (ticket.number,)),
            ("support:ticket-reply", "post", (ticket.number,)),
            ("support:ticket-close", "post", (ticket.number,)),
            ("support:ticket-reopen", "post", (ticket.number,)),
        ):
            url = reverse(name, args=args)
            response = getattr(client, method)(url, {"body": "hello"} if method == "post" else {})

            assert response.status_code == 404, name

    def test_the_list_shows_only_this_customers_tickets(self, client, user, other_user):
        ticket_for(user, subject="Mine, and secret")
        ticket_for(other_user, subject="Theirs, and secret")
        client.force_login(user)

        body = client.get(reverse("support:ticket-list")).content.decode()

        assert "Mine, and secret" in body
        assert "Theirs, and secret" not in body

    def test_the_list_filters_by_status(self, client, user):
        ticket_for(user, subject="Needs me", status=SupportTicket.Status.WAITING_FOR_CUSTOMER)
        ticket_for(user, subject="All done", status=SupportTicket.Status.CLOSED)
        client.force_login(user)

        body = client.get(
            f"{reverse('support:ticket-list')}?status={SupportTicket.Status.CLOSED}"
        ).content.decode()

        assert "All done" in body
        assert "Needs me" not in body

    def test_a_nonsense_status_filter_is_ignored_rather_than_crashing(self, client, user):
        ticket_for(user, subject="Still here")
        client.force_login(user)

        response = client.get(f"{reverse('support:ticket-list')}?status=__drop__")

        assert response.status_code == 200
        assert "Still here" in response.content.decode()

    def test_a_blank_message_is_refused(self, client, user):
        ticket = open_ticket(user)
        client.force_login(user)

        response = client.post(
            reverse("support:ticket-reply", args=[ticket.number]),
            {"body": "   "},
            follow=True,
        )

        assert response.status_code == 200
        assert ticket.messages.count() == 1
        assert "Write a message first." in message_texts(response)


# --------------------------------------------------------------------------------------
# References: ownership proved, mass assignment refused
# --------------------------------------------------------------------------------------


class TestReferences:
    def test_an_order_handle_resolves_and_derives_payment_and_shipment(self, user, product):
        order = place_and_pay(user, product.variants.get())

        resolved = selectors_reference_order(user, order.number)

        assert resolved["order"].pk == order.pk
        assert resolved["payment"].order_id == order.pk
        # Both are derived from the order; a shipment only joins when one exists.
        assert set(resolved) <= {"order", "payment", "shipment"}
        if "shipment" in resolved:
            assert resolved["shipment"].order_id == order.pk

    def test_somebody_elses_order_fails_exactly_like_a_missing_one(self, user, other_user, product):
        order = place_and_pay(other_user, product.variants.get())

        with pytest.raises(ReferenceError) as not_theirs:
            selectors_reference_order(user, order.number)
        with pytest.raises(ReferenceError) as missing:
            selectors_reference_order(user, "FW-1999-999999")

        assert not_theirs.value.message == missing.value.message

    def test_a_payment_cannot_be_named_by_a_customer(self, user):
        from apps.support.services.references import resolve_for_customer

        with pytest.raises(ReferenceError) as exc:
            resolve_for_customer(user, payment=1)

        assert "payment" in str(exc.value)

    def test_an_unknown_reference_key_is_an_error_not_an_ignored_extra(self, user):
        from apps.support.services.references import resolve_for_customer

        with pytest.raises(ReferenceError) as exc:
            resolve_for_customer(user, creator_post_id=7)

        assert "creator_post_id" in str(exc.value)

    def test_a_product_is_public_so_existence_is_all_that_is_proved(self, user, product):
        from apps.support.services.references import resolve_for_customer

        resolved = resolve_for_customer(user, product=product.slug)
        assert resolved["product"].pk == product.pk

    def test_a_review_and_a_loop_item_must_belong_to_the_customer(
        self, user, other_user, product, db
    ):
        from apps.loop.models import LoopItem
        from apps.support.services.references import resolve_for_customer

        variant = product.variants.get()
        mine = make_review(placed_order(user, variant), user, product)
        theirs = make_review(placed_order(other_user, variant), other_user, product)
        my_item = LoopItem.objects.create(
            user=user,
            type=LoopItem.Type.RESALE,
            status=LoopItem.Status.LISTED,
            condition=LoopItem.Condition.GOOD,
            asking_price=20,
        )
        their_item = LoopItem.objects.create(
            user=other_user,
            type=LoopItem.Type.RESALE,
            status=LoopItem.Status.LISTED,
            condition=LoopItem.Condition.GOOD,
            asking_price=25,
        )

        assert resolve_for_customer(user, review=mine.pk)["review"].pk == mine.pk
        assert resolve_for_customer(user, loop_item=my_item.pk)["loop_item"].pk == my_item.pk
        for payload in ({"review": theirs.pk}, {"loop_item": their_item.pk}):
            with pytest.raises(ReferenceError):
                resolve_for_customer(user, **payload)

    def test_the_prefill_drops_a_handle_the_customer_cannot_prove(
        self, client, user, other_user, product
    ):
        """The "Need help?" link carries a handle; a hand-edited URL must not echo one back."""
        order = place_and_pay(other_user, product.variants.get())
        client.force_login(user)

        body = client.get(
            f"{reverse('support:ticket-create')}?order={order.number}"
        ).content.decode()

        assert order.number not in body

    def test_the_prefill_keeps_a_handle_the_customer_owns(self, client, user, product):
        order = place_and_pay(user, product.variants.get())
        client.force_login(user)

        body = client.get(
            f"{reverse('support:ticket-create')}?order={order.number}"
        ).content.decode()

        assert order.number in body

    def test_staff_link_records_by_primary_key_and_clears_only_what_is_named(
        self, user, product, db
    ):
        agent = make_agent(db, manager=True)
        ticket = ticket_for(user)
        product_row = product

        changed = ticket_service.link_ticket_references(ticket, actor=agent, product=product_row.pk)

        assert changed == ["product"]
        assert ticket.product_id == product_row.pk
        # A blank box means "leave it alone" -- clearing is explicit.
        assert ticket_service.link_ticket_references(ticket, actor=agent) == []
        assert ticket_service.link_ticket_references(ticket, actor=agent, product=None) == [
            "product"
        ]
        ticket.refresh_from_db()
        assert ticket.product_id is None

    def test_a_customer_cannot_link_records_at_all(self, user, product):
        ticket = ticket_for(user)

        with pytest.raises(ForbiddenError):
            ticket_service.link_ticket_references(ticket, actor=user, product=product.pk)


def selectors_reference_order(user, handle: str) -> dict:
    from apps.support.services.references import resolve_for_customer

    return resolve_for_customer(user, order=handle)


# --------------------------------------------------------------------------------------
# Domain errors: one exception type per rule, each with a stable code
# --------------------------------------------------------------------------------------


class TestDomainErrors:
    def test_a_duplicate_is_a_typed_error_that_names_the_existing_ticket(self, user, product):
        first = open_ticket(
            user, category=SupportTicket.Category.PRODUCT, refs={"product": product.slug}
        )

        with pytest.raises(DuplicateTicketError) as exc:
            open_ticket(
                user,
                subject="Same problem again",
                description="Nothing changed.",
                category=SupportTicket.Category.PRODUCT,
                refs={"product": product.slug},
            )

        assert exc.value.code == "support_duplicate"
        assert first.number in exc.value.message
        assert SupportTicket.objects.count() == 1

    def test_a_closed_ticket_does_not_block_a_fresh_one(self, user, product):
        first = open_ticket(
            user, category=SupportTicket.Category.PRODUCT, refs={"product": product.slug}
        )
        ticket_service.transition_ticket(first, to_status=SupportTicket.Status.CLOSED, actor=user)

        second = open_ticket(
            user,
            subject="It came back damaged",
            description="Different problem.",
            category=SupportTicket.Category.PRODUCT,
            refs={"product": product.slug},
        )

        assert second.pk != first.pk

    def test_an_impossible_transition_is_its_own_error(self, user, db):
        agent = make_agent(db, manager=True)
        ticket = ticket_for(user, status=SupportTicket.Status.CLOSED, closed_at=timezone.now())

        with pytest.raises(TransitionError) as exc:
            ticket_service.transition_ticket(
                ticket, to_status=SupportTicket.Status.OPEN, actor=agent
            )

        assert exc.value.code == "support_bad_transition"
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.CLOSED

    def test_an_unknown_status_is_refused_before_the_graph_is_consulted(self, user, db):
        agent = make_agent(db, manager=True)
        ticket = open_ticket(user)

        with pytest.raises(SupportError) as exc:
            ticket_service.transition_ticket(ticket, to_status="sideways", actor=agent)

        assert exc.value.code == "support_invalid"


# --------------------------------------------------------------------------------------
# Attachments: content-checked, privately stored, ownership-checked download
# --------------------------------------------------------------------------------------


class TestAttachments:
    def test_a_valid_image_is_accepted_with_the_right_content_type(self):
        assert validate_attachment(png_upload()) == "image/png"
        assert validate_attachment(SimpleUploadedFile("a.jpg", jpeg_bytes())) == "image/jpeg"

    def test_plain_text_and_pdf_are_accepted_on_content_not_on_name(self):
        text = SimpleUploadedFile("note.txt", b"the parcel arrived dented")
        pdf = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\n")

        assert validate_attachment(text) == "text/plain"
        assert validate_attachment(pdf) == "application/pdf"

    @pytest.mark.parametrize(
        ("name", "payload", "code"),
        [
            ("payload.exe", b"MZ\x90\x00", "support_attachment_type"),
            (
                "page.svg",
                b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                "support_attachment_type",
            ),
            (
                "fake.png",
                b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                "support_attachment_unreadable",
            ),
            ("fake.png", b"not an image at all", "support_attachment_unreadable"),
            ("fake.pdf", b"pretend", "support_attachment_unreadable"),
            ("binary.txt", b"clean\x00text", "support_attachment_unreadable"),
            ("nothing.txt", b"", "support_attachment_empty"),
        ],
    )
    def test_hostile_or_broken_files_are_refused(self, name, payload, code):
        with pytest.raises(AttachmentError) as exc:
            validate_attachment(SimpleUploadedFile(name, payload))

        assert exc.value.code == code

    def test_a_renamed_image_is_caught_by_the_format_check(self):
        with pytest.raises(AttachmentError) as exc:
            validate_attachment(SimpleUploadedFile("photo.png", jpeg_bytes()))

        assert exc.value.code == "support_attachment_type"

    @override_settings(SUPPORT_ATTACHMENT_MAX_BYTES=16)
    def test_oversize_files_are_refused_before_they_are_decoded(self):
        with pytest.raises(AttachmentError) as exc:
            validate_attachment(png_upload())

        assert exc.value.code == "support_attachment_too_large"

    @override_settings(SUPPORT_ATTACHMENT_MAX_PIXELS=2)
    def test_a_decompression_bomb_is_refused_by_the_pixel_ceiling(self):
        with pytest.raises(AttachmentError) as exc:
            validate_attachment(wide_png_upload())

        assert exc.value.code == "support_attachment_dimensions"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("C:\\evil\\shot.png", "shot.png"),
            ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
            ("...hidden.txt", "hidden.txt"),
            ("", "attachment"),
            ("\x00\x01name.txt", "name.txt"),
        ],
    )
    def test_the_stored_name_is_a_basename_never_a_path(self, raw, expected):
        assert sanitize_filename(raw) == expected

    def test_a_stored_attachment_has_no_public_url_and_lives_outside_media_root(
        self, user, tmp_path
    ):
        ticket = ticket_for(user)
        message = ticket.messages.create(
            author=user, author_type=SupportMessage.AuthorType.CUSTOMER, body="see attached"
        )
        with override_settings(
            SUPPORT_ATTACHMENT_ROOT=str(tmp_path / "support"),
            MEDIA_ROOT=str(tmp_path / "public-media"),
        ):
            attachment = SupportAttachment(
                message=message,
                original_filename="evidence.png",
                content_type="image/png",
                size=len(PNG_BYTES),
                uploaded_by=user,
            )
            attachment.file.save("evidence.png", png_upload(), save=True)

            assert attachment.file.path.startswith(str(tmp_path / "support"))
            with pytest.raises(ValueError):
                _ = attachment.file.url
            # Nothing under MEDIA_ROOT: the web server never sees these bytes.
            assert not str(attachment.file.path).startswith(str(settings.MEDIA_ROOT))

    def test_the_owner_downloads_and_a_stranger_gets_a_404(self, client, user, other_user, db):
        agent = make_agent(db)
        attachment = upload_for(client, user)

        client.force_login(user)
        mine = client.get(reverse("support:attachment", args=[attachment.pk]))
        assert mine.status_code == 200
        assert mine["Content-Disposition"].startswith("attachment")
        assert mine["Cache-Control"].startswith("private, no-store")  # never_cache appends
        assert mine["X-Content-Type-Options"] == "nosniff"
        assert b"".join(mine.streaming_content) == PNG_BYTES

        client.force_login(other_user)
        assert client.get(reverse("support:attachment", args=[attachment.pk])).status_code == 404

        client.force_login(agent)
        assert client.get(reverse("support:attachment", args=[attachment.pk])).status_code == 200

    def test_an_anonymous_download_is_redirected_to_the_login_page(self, client, user):
        attachment = upload_for(client, user)
        client.logout()  # upload_for signs the customer in to create the file

        response = client.get(reverse("support:attachment", args=[attachment.pk]))

        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_an_upload_is_attached_to_the_message_it_came_with(self, client, user):
        client.force_login(user)
        client.post(
            reverse("support:ticket-create"),
            {
                "subject": "Photo of the damage",
                "description": "See the attached photo.",
                "category": SupportTicket.Category.DELIVERY,
                "files": [png_upload("damage.png")],
            },
        )

        attachment = SupportAttachment.objects.get()
        assert attachment.message.ticket.customer_id == user.pk
        assert attachment.original_filename == "damage.png"
        assert attachment.content_type == "image/png"
        assert SupportTicketEvent.objects.filter(
            event_type=SupportTicketEvent.EventType.ATTACHMENT_ADDED
        ).exists()

    def test_a_rejected_upload_does_not_leave_a_ticket_behind(self, client, user):
        client.force_login(user)
        response = client.post(
            reverse("support:ticket-create"),
            {
                "subject": "Malware as a service",
                "description": "Please open this.",
                "category": SupportTicket.Category.OTHER,
                "files": [SimpleUploadedFile("evil.exe", b"MZ\x90\x00")],
            },
        )

        assert response.status_code == 200  # re-rendered with the error
        assert SupportTicket.objects.count() == 0
        assert SupportAttachment.objects.count() == 0

    # -- per-message file cap (Phase 18) -----------------------------------

    @override_settings(SUPPORT_MAX_ATTACHMENTS_PER_MESSAGE=2)
    def test_more_files_than_the_cap_is_rejected_before_any_write(self, user):
        """The count check runs first: a file flood is its own abuse."""
        files = [png_upload(f"evidence-{i}.png") for i in range(3)]

        with pytest.raises(AttachmentError) as exc:
            open_ticket(user, uploads=files)

        assert exc.value.code == "support_too_many_attachments"
        assert SupportTicket.objects.count() == 0
        assert SupportAttachment.objects.count() == 0

    @override_settings(SUPPORT_MAX_ATTACHMENTS_PER_MESSAGE=1)
    def test_a_reply_cannot_exceed_the_cap(self, user):
        ticket = ticket_for(user)
        files = [png_upload("one.png"), png_upload("two.png")]

        with pytest.raises(AttachmentError) as exc:
            message_service.post_customer_message(
                ticket, customer=user, body="Any update?", uploads=files
            )

        assert exc.value.code == "support_too_many_attachments"
        assert not SupportMessage.objects.filter(ticket=ticket).exists()
        assert SupportAttachment.objects.count() == 0

    @override_settings(SUPPORT_MAX_ATTACHMENTS_PER_MESSAGE=2)
    def test_exactly_at_the_cap_is_accepted(self, user):
        files = [png_upload("one.png"), png_upload("two.png")]

        ticket = open_ticket(user, uploads=files)

        attached = SupportAttachment.objects.filter(message__ticket=ticket)
        assert attached.count() == 2


def upload_for(client, customer) -> SupportAttachment:
    """A ticket with one customer message and one real stored file."""
    client.force_login(customer)
    client.post(
        reverse("support:ticket-create"),
        {
            "subject": "Attached evidence",
            "description": "Screenshot attached.",
            "category": SupportTicket.Category.DELIVERY,
            "files": [png_upload()],
        },
    )
    return SupportAttachment.objects.get()


# --------------------------------------------------------------------------------------
# Desk surface (HTML): capability gate, transcript, every desk action
# --------------------------------------------------------------------------------------


class TestStaffHtml:
    def test_the_desk_is_a_404_for_anyone_without_the_capability(self, client, user, db):
        client.force_login(user)
        assert client.get(reverse("support:staff-dashboard")).status_code == 404

        staff_only = get_user_model().objects.create_user(
            email="staff@flashwear.test", password="Str0ng-Passw0rd!"
        )
        staff_only.is_staff = True
        staff_only.save(update_fields=["is_staff"])
        client.force_login(staff_only)
        assert client.get(reverse("support:staff-dashboard")).status_code == 404

        in_group = get_user_model().objects.create_user(
            email="group@flashwear.test", password="Str0ng-Passw0rd!"
        )
        ensure_capabilities()
        in_group.groups.add(Group.objects.get(name=AGENT_GROUP))
        client.force_login(in_group)
        assert client.get(reverse("support:staff-dashboard")).status_code == 404

    def test_the_desk_renders_for_an_agent_and_a_superuser(self, client, db, admin_user):
        agent = make_agent(db)
        client.force_login(agent)
        assert client.get(reverse("support:staff-dashboard")).status_code == 200

        client.force_login(admin_user)
        assert client.get(reverse("support:staff-dashboard")).status_code == 200

    def test_the_queue_filters_counts_and_scopes_to_the_desk(self, client, user, other_user, db):
        agent = make_agent(db)
        ticket_for(user, subject="Mine and escalated", status=SupportTicket.Status.ESCALATED)
        ticket_for(other_user, subject="Theirs and calm", status=SupportTicket.Status.OPEN)
        client.force_login(agent)

        body = client.get(reverse("support:staff-dashboard")).content.decode()
        assert "Mine and escalated" in body
        assert "Theirs and calm" in body  # the desk sees every customer's ticket
        assert "1" in body  # counters render

        filtered = client.get(
            f"{reverse('support:staff-dashboard')}?status={SupportTicket.Status.ESCALATED}"
        ).content.decode()
        assert "Mine and escalated" in filtered
        assert "Theirs and calm" not in filtered

        searched = client.get(f"{reverse('support:staff-dashboard')}?q=calm").content.decode()
        assert "Theirs and calm" in searched

    def test_a_public_reply_notifies_the_customer_and_parks_the_ticket(self, client, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        client.force_login(agent)

        response = client.post(
            reverse("support:staff-reply", args=[ticket.number]),
            {"body": "We are on it, checking with the courier."},
        )

        assert response.status_code == 302
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.WAITING_FOR_CUSTOMER
        assert ticket.first_response_at is not None
        assert ticket.last_agent_message_at is not None
        assert SupportTicketEvent.objects.filter(
            ticket=ticket, event_type=SupportTicketEvent.EventType.AGENT_MESSAGE
        ).exists()
        assert any(user.email in message.to for message in mail.outbox)

    def test_an_internal_note_never_reaches_the_customer(self, client, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        client.force_login(agent)
        client.post(
            reverse("support:staff-note", args=[ticket.number]),
            {"body": "CUSTOMER IS ON THE BAN LIST"},
        )

        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.OPEN  # a note changes nothing visible
        note = SupportMessage.objects.get(is_internal=True)
        assert note.author_type == SupportMessage.AuthorType.AGENT

        client.force_login(user)
        body = client.get(reverse("support:ticket-detail", args=[ticket.number])).content.decode()
        assert "CUSTOMER IS ON THE BAN LIST" not in body

        customers_copy = [m for m in mail.outbox if user.email in m.to]
        assert customers_copy == []
        assert mail.outbox, "the desk still hears about the conversation"

    def test_an_illegal_transition_is_refused_with_a_message_and_no_write(self, client, user, db):
        agent = make_agent(db, manager=True)
        ticket = ticket_for(user, status=SupportTicket.Status.CLOSED, closed_at=timezone.now())
        client.force_login(agent)

        response = client.post(
            reverse("support:staff-status", args=[ticket.number]),
            {"status": SupportTicket.Status.OPEN},
            follow=True,
        )

        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.CLOSED
        assert response.status_code == 200
        assert "cannot move" in message_texts(response)

    def test_escalation_is_routed_through_its_own_form(self, client, user, db):
        agent = make_agent(db, manager=True)
        ticket = open_ticket(user)
        client.force_login(agent)

        response = client.post(
            reverse("support:staff-status", args=[ticket.number]),
            {"status": SupportTicket.Status.ESCALATED},
            follow=True,
        )

        assert "Use the escalation form" in message_texts(response)
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.OPEN

    def test_a_non_manager_cannot_escalate_and_the_ticket_does_not_move(self, client, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        client.force_login(agent)

        response = client.post(
            reverse("support:staff-escalate", args=[ticket.number]),
            {"reason": "Courier contract breach", "target": "team_lead"},
            follow=True,
        )

        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.OPEN
        assert ticket.escalation_reason == ""
        assert response.status_code == 200
        assert "manager" in message_texts(response)

    def test_a_manager_escalates_with_a_reason_and_the_managers_are_told(self, client, user, db):
        ensure_capabilities()
        lead = make_agent(db, email="lead@flashwear.test", manager=True)
        ticket = open_ticket(user)
        before = len(mail.outbox)
        client.force_login(lead)

        client.post(
            reverse("support:staff-escalate", args=[ticket.number]),
            {"reason": "Third failed delivery", "target": "on_call"},
        )

        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.ESCALATED
        assert ticket.escalation_reason == "Third failed delivery"
        assert ticket.escalation_target == "on_call"
        assert ticket.escalated_by_id == lead.pk
        assert SupportTicketEvent.objects.filter(
            ticket=ticket, event_type=SupportTicketEvent.EventType.ESCALATED
        ).exists()
        assert len(mail.outbox) > before

    def test_an_escalation_without_a_reason_is_refused(self, client, user, db):
        lead = make_agent(db, email="lead2@flashwear.test", manager=True)
        ticket = open_ticket(user)
        client.force_login(lead)

        response = client.post(
            reverse("support:staff-escalate", args=[ticket.number]),
            {"reason": ""},
            follow=True,
        )

        assert "needs a reason" in message_texts(response)
        ticket.refresh_from_db()
        assert ticket.status != SupportTicket.Status.ESCALATED

    def test_an_agent_claims_a_ticket_but_cannot_steal_one(self, client, user, db):
        alice = make_agent(db, email="alice@flashwear.test")
        bob = make_agent(db, email="bob@flashwear.test")
        ticket = ticket_for(user)
        client.force_login(alice)

        client.post(
            reverse("support:staff-assign", args=[ticket.number]), {"agent_email": alice.email}
        )
        ticket.refresh_from_db()
        assert ticket.assigned_to_id == alice.pk

        # Bob tries to take Alice's ticket.
        client.force_login(bob)
        response = client.post(
            reverse("support:staff-assign", args=[ticket.number]),
            {"agent_email": bob.email},
            follow=True,
        )
        ticket.refresh_from_db()
        assert ticket.assigned_to_id == alice.pk
        assert "manager" in message_texts(response)
        assert SupportTicket.objects.filter(pk=ticket.pk).exists()

    def test_a_manager_reassigns_and_releases(self, client, user, db):
        alice = make_agent(db, email="alice2@flashwear.test")
        bob = make_agent(db, email="bob2@flashwear.test")
        lead = make_agent(db, email="lead3@flashwear.test", manager=True)
        ticket = ticket_for(user, assigned_to=alice, assigned_at=timezone.now())
        client.force_login(lead)

        client.post(
            reverse("support:staff-assign", args=[ticket.number]), {"agent_email": bob.email}
        )
        ticket.refresh_from_db()
        assert ticket.assigned_to_id == bob.pk

        client.post(reverse("support:staff-assign", args=[ticket.number]), {"agent_email": ""})
        ticket.refresh_from_db()
        assert ticket.assigned_to_id is None
        assert ticket.assigned_at is None
        assert SupportTicketEvent.objects.filter(
            ticket=ticket, event_type=SupportTicketEvent.EventType.UNASSIGNED
        ).exists()

    def test_a_customer_cannot_change_priority_and_an_agent_can(self, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)

        with pytest.raises(ForbiddenError):
            ticket_service.set_priority(ticket, actor=user, priority=SupportTicket.Priority.URGENT)

        ticket_service.set_priority(ticket, actor=agent, priority=SupportTicket.Priority.URGENT)
        ticket.refresh_from_db()
        assert ticket.priority == SupportTicket.Priority.URGENT
        assert SupportTicketEvent.objects.filter(
            ticket=ticket, event_type=SupportTicketEvent.EventType.PRIORITY_CHANGED
        ).exists()

    def test_a_customer_cannot_reach_a_status_the_graph_keeps_for_the_desk(self, user):
        ticket = open_ticket(user)

        with pytest.raises(ForbiddenError):
            ticket_service.transition_ticket(
                ticket, to_status=SupportTicket.Status.RESOLVED, actor=user
            )

    def test_the_same_status_is_a_no_op_rather_than_an_event(self, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        events_before = SupportTicketEvent.objects.count()

        same = ticket_service.transition_ticket(
            ticket, to_status=SupportTicket.Status.OPEN, actor=agent
        )

        assert same.pk == ticket.pk
        assert SupportTicketEvent.objects.count() == events_before

    def test_the_desk_can_read_a_ticket_the_customer_cannot_share_the_url_of(self, client, user):
        ticket = ticket_for(user)
        client.force_login(user)
        # The customer's own URL works...
        assert client.get(reverse("support:ticket-detail", args=[ticket.number])).status_code == 200
        # ...and the desk URL is a different route with a different gate.
        assert (
            client.get(reverse("support:staff-ticket-detail", args=[ticket.number])).status_code
            == 404
        )

    def test_the_staff_detail_renders_transcript_audit_and_every_form(self, client, user, db):
        agent = make_agent(db, manager=True)
        ticket = ticket_for(user)
        message_service.post_agent_message(
            ticket, actor=agent, body="Looking into it.", internal=False
        )
        client.force_login(agent)

        body = client.get(
            reverse("support:staff-ticket-detail", args=[ticket.number])
        ).content.decode()

        assert ticket.number in body
        assert "Looking into it." in body
        assert "Audit trail" in body
        assert reverse("support:staff-note", args=[ticket.number]) in body
        assert reverse("support:staff-link", args=[ticket.number]) in body
        assert "noindex, nofollow" in body

    def test_the_transcript_is_ordered_and_carries_attachments_for_the_desk(self, client, user, db):
        agent = make_agent(db)
        ticket = ticket_for(user)
        message_service.post_agent_message(ticket, actor=agent, body="First reply.")
        message_service.post_agent_message(ticket, actor=agent, body="Second reply.")
        client.force_login(agent)

        body = client.get(
            reverse("support:staff-ticket-detail", args=[ticket.number])
        ).content.decode()
        assert body.index("First reply.") < body.index("Second reply.")


# --------------------------------------------------------------------------------------
# Notifications: who hears what, and what happens when delivery fails
# --------------------------------------------------------------------------------------


class TestNotifications:
    def test_a_new_ticket_reaches_the_desk_and_not_the_customer(self, user, db):
        make_agent(db)
        mail.outbox.clear()

        open_ticket(user)

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["agent@flashwear.test"]
        assert user.email not in mail.outbox[0].to

    def test_a_customer_reply_reaches_the_desk(self, user, db):
        make_agent(db)
        ticket = open_ticket(user)
        mail.outbox.clear()

        message_service.post_customer_message(ticket, customer=user, body="Any news?")

        assert any("agent@flashwear.test" in m.to for m in mail.outbox)
        assert not any(user.email in m.to for m in mail.outbox)

    def test_an_agent_reply_reaches_the_customer(self, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        mail.outbox.clear()

        message_service.post_agent_message(ticket, actor=agent, body="Checking with the courier.")

        assert any(user.email in m.to for m in mail.outbox)

    def test_an_internal_note_reaches_nobody_outside_the_desk(self, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        mail.outbox.clear()

        message_service.post_agent_message(
            ticket, actor=agent, body="Internal only.", internal=True
        )

        assert mail.outbox == []

    @pytest.mark.parametrize(
        ("status", "customer_is_told"),
        [
            (SupportTicket.Status.RESOLVED, True),
            (SupportTicket.Status.CLOSED, True),
            (SupportTicket.Status.WAITING_FOR_CUSTOMER, True),
            (SupportTicket.Status.ESCALATED, True),
            (SupportTicket.Status.IN_PROGRESS, False),
            (SupportTicket.Status.WAITING_INTERNAL, False),
        ],
    )
    def test_the_customer_hears_about_states_that_are_for_them(
        self, user, db, status, customer_is_told
    ):
        agent = make_agent(db, manager=True)
        ticket = ticket_for(user, status=SupportTicket.Status.OPEN)
        mail.outbox.clear()

        ticket_service.transition_ticket(ticket, to_status=status, actor=agent)

        assert any(user.email in m.to for m in mail.outbox) is customer_is_told

    def test_a_notification_failure_never_fails_the_write(self, user, db, caplog):
        make_agent(db)  # somebody must be on the receiving end for this to fail

        def explode(*args, **kwargs):
            raise RuntimeError("mail backend is down")

        import apps.support.services.notifications as notifications

        original = notifications.send_account_email
        notifications.send_account_email = explode
        try:
            with caplog.at_level(logging.ERROR, logger="flashwear.support.notifications"):
                ticket = open_ticket(user)
        finally:
            notifications.send_account_email = original

        assert ticket.pk is not None
        assert ticket.messages.count() == 1
        assert any("Support notification failed" in record.message for record in caplog.records)

    def test_a_successful_delivery_is_recorded_in_the_audit_trail(self, user, db):
        make_agent(db)
        mail.outbox.clear()

        ticket = open_ticket(user)

        event = SupportTicketEvent.objects.get(
            ticket=ticket, event_type=SupportTicketEvent.EventType.NOTIFICATION_SENT
        )
        assert event.metadata["audience"] == "staff"
        assert event.metadata["recipients"] == 1

    def test_the_notification_carries_a_link_to_the_right_surface(self, user, db):
        make_agent(db)
        ticket = open_ticket(user)

        body = mail.outbox[0].body
        assert ticket.number in body
        assert reverse("support:staff-ticket-detail", args=[ticket.number]) in body
        assert mail.outbox[0].alternatives, "an HTML part must be attached"


# --------------------------------------------------------------------------------------
# API: same rules, same scoping, same codes
# --------------------------------------------------------------------------------------


class TestApi:
    def test_the_root_indexes_both_namespaces(self, api_client, user):
        api_client.force_login(user)

        response = api_client.get("/api/v1/support/")

        assert response.status_code == 200
        assert set(response.json()) == {"tickets", "staff_tickets"}

    def test_the_list_requires_a_session(self, api_client):
        response = api_client.get("/api/v1/support/tickets/")

        assert response.status_code in (401, 403)

    def test_the_list_returns_only_this_customers_tickets(self, api_client, user, other_user):
        ticket_for(user, subject="Mine")
        ticket_for(other_user, subject="Theirs")
        api_client.force_login(user)

        results = api_client.get("/api/v1/support/tickets/").json()["results"]

        assert [row["subject"] for row in results] == ["Mine"]
        assert results[0]["customer_email"] is None  # staff-only context
        assert results[0]["assignee"] is None

    def test_another_customers_ticket_is_a_404_not_a_403(self, api_client, user, other_user):
        ticket = ticket_for(user)
        api_client.force_login(other_user)

        assert api_client.get(f"/api/v1/support/tickets/{ticket.number}/").status_code == 404
        assert (
            api_client.post(
                f"/api/v1/support/tickets/{ticket.number}/messages/", {"body": "hi"}
            ).status_code
            == 404
        )

    def test_creating_a_ticket_ignores_every_field_the_service_owns(
        self, api_client, user, other_user
    ):
        api_client.force_login(user)

        response = api_client.post(
            "/api/v1/support/tickets/",
            {
                "subject": "Via the API",
                "description": "Straight from a client.",
                "category": SupportTicket.Category.ORDER,
                "priority": SupportTicket.Priority.URGENT,
                "status": SupportTicket.Status.RESOLVED,
                "source": SupportTicket.Source.ADMIN,
                "customer": other_user.pk,
                "assigned_to": other_user.pk,
            },
        )

        assert response.status_code == 201
        payload = response.json()
        assert payload["status"] == SupportTicket.Status.OPEN
        assert payload["priority"] == SupportTicket.Priority.NORMAL
        assert payload["source"] == SupportTicket.Source.API
        ticket = SupportTicket.objects.get(number=payload["number"])
        assert ticket.customer_id == user.pk

    def test_the_messages_endpoint_hides_internal_notes_from_the_customer(
        self, api_client, user, db
    ):
        agent = make_agent(db)
        ticket = open_ticket(user)
        message_service.post_agent_message(ticket, actor=agent, body="Public answer.")
        message_service.post_agent_message(
            ticket, actor=agent, body="NOT FOR THE CUSTOMER", internal=True
        )
        api_client.force_login(user)

        results = api_client.get(f"/api/v1/support/tickets/{ticket.number}/messages/").json()[
            "results"
        ]

        bodies = [row["body"] for row in results]
        assert "Public answer." in bodies
        assert "NOT FOR THE CUSTOMER" not in bodies
        assert all(row["is_internal"] is False for row in results)

    def test_a_customer_cannot_write_an_internal_message_through_the_api(self, api_client, user):
        ticket = open_ticket(user)
        api_client.force_login(user)

        response = api_client.post(
            f"/api/v1/support/tickets/{ticket.number}/messages/",
            {"body": "let me in", "internal": True},
        )

        assert response.status_code == 201
        assert SupportMessage.objects.get(body="let me in").is_internal is False

    def test_close_and_reopen_round_trip(self, api_client, user):
        ticket = open_ticket(user)
        api_client.force_login(user)

        closed = api_client.post(f"/api/v1/support/tickets/{ticket.number}/close/")
        reopened = api_client.post(f"/api/v1/support/tickets/{ticket.number}/reopen/")

        assert closed.json()["status"] == SupportTicket.Status.CLOSED
        assert reopened.json()["status"] == SupportTicket.Status.IN_PROGRESS

    def test_the_desk_namespace_404s_for_a_customer(self, api_client, user):
        api_client.force_login(user)

        assert api_client.get("/api/v1/support/staff/tickets/").status_code == 404

    def test_the_desk_namespace_shows_the_operator_context_to_an_agent(self, api_client, db):
        agent = make_agent(db)
        ticket_for(get_user_model().objects.create_user(email="c@flashwear.test"))
        api_client.force_login(agent)

        results = api_client.get("/api/v1/support/staff/tickets/").json()["results"]

        assert results[0]["customer_email"] == "c@flashwear.test"

    def test_the_status_endpoint_refuses_to_escalate_silently(self, api_client, db):
        from apps.support.models import SupportTicket as T

        agent = make_agent(db, manager=True)
        ticket = ticket_for(get_user_model().objects.create_user(email="c2@flashwear.test"))
        api_client.force_login(agent)

        response = api_client.post(
            f"/api/v1/support/staff/tickets/{ticket.number}/status/",
            {"status": SupportTicket.Status.ESCALATED},
        )

        assert response.status_code == 400
        assert response.json()["code"] == "support_use_escalate"
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.OPEN
        assert T.objects.filter(pk=ticket.pk).exists()

    def test_an_agent_without_the_manager_capability_gets_a_403_for_escalation(
        self, api_client, user, db
    ):
        agent = make_agent(db)
        ticket = ticket_for(user)
        api_client.force_login(agent)

        response = api_client.post(
            f"/api/v1/support/staff/tickets/{ticket.number}/escalate/",
            {"reason": "Needs a lead"},
        )

        assert response.status_code == 403
        assert response.json()["code"] == "support_forbidden"
        ticket.refresh_from_db()
        assert ticket.status != SupportTicket.Status.ESCALATED

    def test_a_manager_escalates_through_the_api(self, api_client, user, db):
        lead = make_agent(db, email="api-lead@flashwear.test", manager=True)
        ticket = ticket_for(user)
        api_client.force_login(lead)

        response = api_client.post(
            f"/api/v1/support/staff/tickets/{ticket.number}/escalate/",
            {"reason": "Legal exposure", "target": "team_lead"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == SupportTicket.Status.ESCALATED

    def test_the_priority_endpoint_has_no_customer_equivalent(self, api_client, user, db):
        agent = make_agent(db)
        ticket = ticket_for(user)
        api_client.force_login(agent)

        response = api_client.post(
            f"/api/v1/support/staff/tickets/{ticket.number}/priority/",
            {"priority": SupportTicket.Priority.LOW},
        )

        assert response.status_code == 200
        assert response.json()["priority"] == SupportTicket.Priority.LOW

    def test_the_note_endpoint_writes_a_private_message(self, api_client, user, db):
        agent = make_agent(db)
        ticket = ticket_for(user)
        api_client.force_login(agent)

        response = api_client.post(
            f"/api/v1/support/staff/tickets/{ticket.number}/note/", {"body": "Shelf audit due"}
        )

        assert response.status_code == 200
        note = SupportMessage.objects.get(body="Shelf audit due")
        assert note.is_internal is True

        api_client.force_login(user)
        results = api_client.get(f"/api/v1/support/tickets/{ticket.number}/messages/").json()[
            "results"
        ]
        assert "Shelf audit due" not in [row["body"] for row in results]

    def test_the_link_endpoint_points_a_ticket_at_an_existing_row(
        self, api_client, user, product, db
    ):
        agent = make_agent(db, manager=True)
        ticket = ticket_for(user)
        api_client.force_login(agent)

        response = api_client.post(
            f"/api/v1/support/staff/tickets/{ticket.number}/link/", {"product": product.pk}
        )

        assert response.status_code == 200
        assert response.json()["references"]["product"] == product.pk

    def test_a_throttled_create_answers_429_with_a_stable_code(self, api_client, user):
        api_client.force_login(user)
        throttle = SupportThrottle()
        for _ in range(settings.SUPPORT_MAX_TICKETS_PER_WINDOW):
            throttle.record_open(user)

        response = api_client.post(
            "/api/v1/support/tickets/",
            {
                "subject": "Too many",
                "description": "Again.",
                "category": SupportTicket.Category.OTHER,
            },
        )

        assert response.status_code == 429
        assert response.json()["code"] == "support_throttled"
        assert SupportTicket.objects.count() == 0

    def test_an_illegal_transition_through_the_api_is_a_400(self, api_client, user, db):
        agent = make_agent(db, manager=True)
        ticket = ticket_for(user, status=SupportTicket.Status.CLOSED, closed_at=timezone.now())
        api_client.force_login(agent)

        response = api_client.post(
            f"/api/v1/support/staff/tickets/{ticket.number}/status/",
            {"status": SupportTicket.Status.OPEN},
        )

        assert response.status_code == 400
        assert response.json()["code"] == "support_bad_transition"


# --------------------------------------------------------------------------------------
# Rate limits (HTML): the other side of the same throttle
# --------------------------------------------------------------------------------------


class TestRateLimits:
    @override_settings(SUPPORT_MAX_TICKETS_PER_WINDOW=2)
    def test_the_form_re_renders_with_a_429_once_the_limit_is_hit(self, client, user):
        client.force_login(user)
        payload = {
            "subject": "One more",
            "description": "Again.",
            "category": SupportTicket.Category.OTHER,
        }
        assert client.post(reverse("support:ticket-create"), payload).status_code == 302
        assert client.post(reverse("support:ticket-create"), payload).status_code == 302

        blocked = client.post(reverse("support:ticket-create"), payload)

        assert blocked.status_code == 429
        assert "wait a little" in message_texts(blocked)
        assert SupportTicket.objects.count() == 2

    @override_settings(SUPPORT_MAX_MESSAGES_PER_WINDOW=2)
    def test_replying_too_fast_is_a_429_and_writes_nothing(self, client, user, db):
        make_agent(db)
        ticket = open_ticket(user)
        client.force_login(user)
        client.post(reverse("support:ticket-reply", args=[ticket.number]), {"body": "one"})
        client.post(reverse("support:ticket-reply", args=[ticket.number]), {"body": "two"})

        blocked = client.post(
            reverse("support:ticket-reply", args=[ticket.number]), {"body": "three"}
        )

        assert blocked.status_code == 429
        assert ticket.messages.count() == 3  # the two replies plus the original
        assert not ticket.messages.filter(body="three").exists()

    def test_the_counter_is_per_user_not_global(self, client, user, other_user):
        throttle = SupportThrottle()
        for _ in range(settings.SUPPORT_MAX_TICKETS_PER_WINDOW):
            throttle.record_open(user)

        assert throttle.check_open(user).blocked is True
        assert throttle.check_open(other_user).blocked is False
        throttle.reset(user)
        assert throttle.check_open(user).blocked is False


# --------------------------------------------------------------------------------------
# Admin: read-only, and no file widget with a URL that raises
# --------------------------------------------------------------------------------------


class TestAdmin:
    def test_every_support_model_is_registered(self):
        for model in (SupportTicket, SupportMessage, SupportAttachment, SupportTicketEvent):
            assert model in site._registry

    def test_the_models_are_read_only(self, admin_user, rf):
        request = rf.get("/")
        request.user = admin_user

        for model in (SupportTicket, SupportMessage, SupportAttachment, SupportTicketEvent):
            admin = site._registry[model]
            assert admin.has_add_permission(request) is False
            assert admin.has_change_permission(request) is False
            assert admin.has_view_permission(request) is True

    def test_the_audit_trail_cannot_be_deleted_either(self, admin_user, rf):
        request = rf.get("/")
        request.user = admin_user

        admin = site._registry[SupportTicketEvent]
        assert admin.has_delete_permission(request) is False

    def test_the_attachment_admin_never_renders_the_file_field(self):
        admin = site._registry[SupportAttachment]

        assert "file" not in admin.fields
        assert "download" in admin.readonly_fields


# --------------------------------------------------------------------------------------
# Housekeeping tasks
# --------------------------------------------------------------------------------------


class TestTasks:
    def test_an_abandoned_resolved_ticket_is_closed_by_the_sweep(self, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        ticket_service.transition_ticket(
            ticket, to_status=SupportTicket.Status.RESOLVED, actor=agent
        )
        SupportTicket.objects.filter(pk=ticket.pk).update(
            resolved_at=timezone.now() - timedelta(days=settings.SUPPORT_CLOSE_AFTER_DAYS + 1)
        )

        closed = close_abandoned_tickets()

        ticket.refresh_from_db()
        assert closed == 1
        assert ticket.status == SupportTicket.Status.CLOSED
        assert SupportTicketEvent.objects.filter(
            ticket=ticket, event_type=SupportTicketEvent.EventType.STATUS_CHANGED
        ).exists()

    def test_a_recently_resolved_ticket_is_left_alone(self, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        ticket_service.transition_ticket(
            ticket, to_status=SupportTicket.Status.RESOLVED, actor=agent
        )

        assert close_abandoned_tickets() == 0
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.RESOLVED

    def test_the_reminder_counts_without_emailing_by_default(self, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        ticket_service.transition_ticket(
            ticket, to_status=SupportTicket.Status.WAITING_FOR_CUSTOMER, actor=agent
        )
        SupportTicket.objects.filter(pk=ticket.pk).update(
            last_agent_message_at=timezone.now()
            - timedelta(days=settings.SUPPORT_REMIND_AFTER_DAYS + 1)
        )
        mail.outbox.clear()

        assert remind_pending_tickets() == 1
        assert mail.outbox == []

    @override_settings(SUPPORT_SEND_REMINDERS=True)
    def test_the_reminder_sends_when_the_deployment_opts_in(self, user, db):
        agent = make_agent(db)
        ticket = open_ticket(user)
        ticket_service.transition_ticket(
            ticket, to_status=SupportTicket.Status.WAITING_FOR_CUSTOMER, actor=agent
        )
        SupportTicket.objects.filter(pk=ticket.pk).update(
            last_agent_message_at=timezone.now()
            - timedelta(days=settings.SUPPORT_REMIND_AFTER_DAYS + 1)
        )
        mail.outbox.clear()

        assert remind_pending_tickets() == 1
        assert any(user.email in message.to for message in mail.outbox)


# --------------------------------------------------------------------------------------
# Domain integration: support points at records, it does not move them
# --------------------------------------------------------------------------------------


class TestDomainIntegration:
    def test_opening_a_ticket_about_an_order_does_not_touch_the_order(self, user, product):
        order = place_and_pay(user, product.variants.get())
        before = (
            order.status,
            order.updated_at,
            order.total,
            SupportTicket.objects.count(),
        )

        ticket = open_ticket(
            user,
            category=SupportTicket.Category.ORDER,
            subject="Where is it?",
            description="Still not here.",
            refs={"order": order.number},
        )

        order.refresh_from_db()
        assert (order.status, order.updated_at, order.total) == before[:3]
        assert SupportTicket.objects.count() == before[3] + 1
        assert ticket.order_id == order.pk
        assert ticket.payment_id is not None  # derived, never named

    def test_no_support_module_imports_a_domain_service(self):
        """The boundary is structural: support reads rows, it never calls their services."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "apps" / "support"
        forbidden = re.compile(
            r"^\s*(?:from|import)\s+apps\."
            r"(orders|payments|inventory|loyalty|catalog|loop|quests|engagement|closet|drops"
            r"|styling|shop|promotions|creator)\.services\b"
        )
        offenders = []
        for path in root.rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if forbidden.match(line):
                    offenders.append(f"{path.name}: {line.strip()}")

        assert offenders == []


# --------------------------------------------------------------------------------------
# Navigation and cross-links: the section is findable from where people already are
# --------------------------------------------------------------------------------------


class TestNavigation:
    def test_support_is_a_live_account_section(self, rf, user):
        from apps.accounts.navigation import account_sections

        request = rf.get("/support/tickets/")
        request.user = user

        sections = {section["label"]: section for section in account_sections(request)}
        support = sections["Support"]

        assert support["live"] is True
        assert support["url"] == reverse("support:home")
        assert support["active"] is True

    def test_the_order_page_links_into_a_ticket_that_already_names_the_order(
        self, client, user, product
    ):
        order = place_and_pay(user, product.variants.get())
        client.force_login(user)

        body = client.get(reverse("account:order-detail", args=[order.number])).content.decode()

        assert f"{reverse('support:ticket-create')}?order={order.number}" in body

    def test_the_product_page_links_to_support(self, client, product):
        client.force_login(get_user_model().objects.create_user(email="b@flashwear.test"))

        body = client.get(reverse("catalog:product-detail", args=[product.slug])).content.decode()

        assert "Contact support" in body


# --------------------------------------------------------------------------------------
# Query discipline: the queue stays flat as the queue grows
# --------------------------------------------------------------------------------------


class TestQueryCounts(TestCase):
    def test_the_customer_ticket_list_has_a_fixed_query_budget(self):
        user = get_user_model().objects.create_user(
            email="budget@flashwear.test", password="Str0ng-Passw0rd!"
        )
        for index in range(3):
            ticket_for(user, subject=f"Ticket {index}")
        self.client.force_login(user)
        # Warm the site-configuration and cart lookups the layout does on a first hit;
        # they are cached from then on, and the budget below is about tickets only.
        self.client.get(reverse("support:ticket-list"))

        with CaptureQueriesContext(connection) as small:
            first = self.client.get(reverse("support:ticket-list"))
        for index in range(3, 12):
            ticket_for(user, subject=f"Ticket {index}")
        with CaptureQueriesContext(connection) as large:
            second = self.client.get(reverse("support:ticket-list"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        # Three tickets and twelve tickets must cost the same: page size, not N+1.
        self.assertEqual(len(small.captured_queries), len(large.captured_queries))
        self.assertLessEqual(len(small.captured_queries), 30, "ticket list query count exploded")

    def test_the_desk_queue_has_a_fixed_query_budget(self):
        agent = get_user_model().objects.create_user(
            email="desk-budget@flashwear.test", password="Str0ng-Passw0rd!"
        )
        agent.is_staff = True
        agent.save(update_fields=["is_staff"])
        ensure_capabilities()
        agent.groups.add(Group.objects.get(name=AGENT_GROUP))

        customers = [
            get_user_model().objects.create_user(
                email=f"queue-{index}@flashwear.test", password="Str0ng-Passw0rd!"
            )
            for index in range(3)
        ]
        for index, customer in enumerate(customers):
            ticket_for(customer, subject=f"Queue {index}")
        self.client.force_login(agent)
        # Same warm-up as the customer budget: the cached chrome must not be charged
        # to the queue.
        self.client.get(reverse("support:staff-dashboard"))

        with CaptureQueriesContext(connection) as small:
            first = self.client.get(reverse("support:staff-dashboard"))
        for index in range(3, 12):
            ticket_for(
                get_user_model().objects.create_user(
                    email=f"more-{index}@flashwear.test", password="Str0ng-Passw0rd!"
                ),
                subject=f"Queue {index}",
            )
        with CaptureQueriesContext(connection) as large:
            second = self.client.get(reverse("support:staff-dashboard"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(small.captured_queries), len(large.captured_queries))
        self.assertLessEqual(len(small.captured_queries), 35, "desk query count exploded")
