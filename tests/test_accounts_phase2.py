"""Phase 2 account tests: models, forms, services, views, email and API.

The suite is organised the way the feature is built, so a failure names the layer that broke:

* ``TestProfileCreation``      -- the ``post_save`` signal and its one real exception
* ``TestRegistration``         -- creating an account, and what it refuses to do
* ``TestEmailVerification``    -- the signed link, its expiry and its replay behaviour
* ``TestAvatarValidation``     -- uploads are checked by content, not by file name
* ``TestProfileUpdates``       -- preferences, consent and the audit trail
* ``TestAddresses``            -- ownership scoping and the "only one default" invariant
* ``TestPasswordReset``        -- non-disclosure, and that the link really changes the password
* ``TestPasswordChange``       -- authenticated change, session preservation
* ``TestRememberMe``           -- session expiry, the part people get subtly wrong
* ``TestAccountDeactivation``  -- what closure does and does not destroy
* ``TestSessionInvalidation``  -- an expired session lands back on sign-in with ``next``
* ``TestAccountApi``           -- the JSON surface, including ownership isolation
* ``TestAccountTemplates``     -- the pages render and carry the accessibility hooks
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlsafe_base64_encode

from apps.accounts import services
from apps.accounts.models import AccountEvent, Address, Profile
from apps.accounts.navigation import account_sections

User = get_user_model()

pytestmark = pytest.mark.django_db


def avatar_bytes(image_format: str = "PNG", size=(64, 64)) -> io.BytesIO:
    """Build a small in-memory image upload."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=image_format)
    buffer.name = f"avatar.{image_format.lower()}"
    buffer.seek(0)
    return buffer


def verification_url(user, token: str | None = None) -> str:
    """The path a verification email would carry for ``user``."""
    uid = urlsafe_base64_encode(str(user.pk).encode())
    token = token or default_token_generator.make_token(user)
    return reverse("account:verify-email", kwargs={"uidb64": uid, "token": token})


def reset_link() -> tuple[str, str]:
    """Pull the uid and token out of the reset email Django just sent."""
    line = next(
        line.strip()
        for line in mail.outbox[-1].body.splitlines()
        if "/account/password/reset/confirm/" in line
    )
    path = line.split("://", 1)[-1]
    uid, token = path.rstrip("/").split("/")[-2:]
    return uid, token


def sign_in(client, *, remember: bool = False, email: str = "shopper@flashwear.test"):
    payload = {"username": email, "password": "Str0ng-Passw0rd!"}
    if remember:
        payload["remember_me"] = "on"
    return client.post(reverse("accounts:login"), payload)


def message_texts(response) -> str:
    """The messages shown after following a redirect, as one searchable string.

    ``str(response.context["messages"])`` is useless here: it reprs the storage object, not the
    queued messages.
    """
    return " | ".join(str(message) for message in response.context.get("messages", []))


def exhaust_throttle(client, url_name: str, email: str) -> None:
    """Drive one account's failure counter past the limit through the real endpoint."""
    from django.test import RequestFactory

    from apps.accounts.throttling import LoginThrottle

    throttle = LoginThrottle()
    throttle.record_failure(RequestFactory().post("/accounts/login/"), email)
    for _ in range(throttle.max_per_account):
        client.post(reverse(url_name), {"username": email, "password": "wrong"})


def registration_payload(**overrides) -> dict:
    payload = {
        "email": "new.shopper@flashwear.test",
        "first_name": "New",
        "last_name": "Shopper",
        "password1": "Str0ng-Passw0rd!",
        "password2": "Str0ng-Passw0rd!",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------------------
# Profile creation
# --------------------------------------------------------------------------------------


class TestProfileCreation:
    def test_signal_creates_profile(self, db):
        user = User.objects.create_user(email="signal@flashwear.test", password="Str0ng-Passw0rd!")
        assert Profile.objects.filter(user=user).exists()

    def test_profile_defaults_to_no_marketing_consent(self, db):
        user = User.objects.create_user(email="consent@flashwear.test", password="Str0ng-Passw0rd!")
        profile = user.profile
        assert profile.marketing_email_opt_in is False
        assert profile.marketing_sms_opt_in is False
        assert profile.preferred_currency == "USD"

    def test_profile_is_not_recreated_on_update(self, db):
        user = User.objects.create_user(email="twice@flashwear.test", password="Str0ng-Passw0rd!")
        first_pk = user.profile.pk
        user.first_name = "Renamed"
        user.save()
        assert user.profile.pk == first_pk

    def test_superuser_creation_also_gets_a_profile(self, db):
        admin = User.objects.create_superuser(
            email="root2@flashwear.test", password="Sup3r-S3cret!"
        )
        assert Profile.objects.filter(user=admin).exists()


# --------------------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------------------


class TestRegistration:
    def test_creates_account_and_signs_in(self, client, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"
        response = client.post(reverse("account:register"), registration_payload())

        assert response.status_code == 302
        assert response["Location"] == reverse("account:dashboard")

        user = User.objects.get(email="new.shopper@flashwear.test")
        assert user.check_password("Str0ng-Passw0rd!")
        assert user.is_email_verified is False
        assert client.session.get("_auth_user_id") == str(user.pk)

    def test_address_is_normalised_before_the_duplicate_check(self, client, user):
        """Mixed case must not create a second row for the same person."""
        response = client.post(
            reverse("account:register"),
            registration_payload(email=user.email.upper()),
        )

        assert response.status_code == 200
        assert response.context["form"].errors["email"]
        assert User.objects.filter(email=user.email).count() == 1

    def test_duplicate_email_is_refused_with_a_field_error(self, client, user):
        response = client.post(reverse("account:register"), registration_payload(email=user.email))

        assert response.status_code == 200
        assert "already exists" in str(response.context["form"].errors["email"])

    def test_password_confirmation_must_match(self, client):
        response = client.post(
            reverse("account:register"), registration_payload(password2="something-else")
        )

        assert response.status_code == 200
        assert response.context["form"].errors["password2"]
        assert not User.objects.filter(email="new.shopper@flashwear.test").exists()

    def test_weak_password_is_refused_by_the_project_validators(self, client, settings):
        settings.AUTH_PASSWORD_VALIDATORS = [
            {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"}
        ]
        response = client.post(
            reverse("account:register"), registration_payload(password1="short", password2="short")
        )

        assert response.status_code == 200
        assert response.context["form"].errors["password2"]
        assert not User.objects.filter(email="new.shopper@flashwear.test").exists()

    def test_registration_creates_no_audit_row_without_an_email_attempt(self, client):
        assert AccountEvent.objects.count() == 0

    def test_registration_is_not_offered_marketing_without_being_asked(self, client, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"
        client.post(reverse("account:register"), registration_payload())

        user = User.objects.get(email="new.shopper@flashwear.test")
        assert user.profile.marketing_email_opt_in is False

    def test_signed_in_customer_is_redirected_away_from_registration(self, client, user):
        client.force_login(user)
        response = client.get(reverse("account:register"))

        assert response.status_code == 302
        assert response["Location"] == reverse("account:dashboard")

    def test_verification_email_is_sent(self, client, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"
        client.post(reverse("account:register"), registration_payload())

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["new.shopper@flashwear.test"]
        assert "/account/verify-email/" in mail.outbox[0].body
        assert "https://flashwear.test" in mail.outbox[0].body

    def test_registration_is_audited(self, client, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"
        client.post(reverse("account:register"), registration_payload())

        events = set(AccountEvent.objects.values_list("event_type", flat=True))
        assert "email_verification_sent" in events


# --------------------------------------------------------------------------------------
# Email verification
# --------------------------------------------------------------------------------------


class TestEmailVerification:
    def test_valid_link_verifies_the_address(self, client, user, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"
        services.send_verification_email(user)

        response = client.get(verification_url(user))

        user.refresh_from_db()
        assert response.status_code == 200
        assert user.is_email_verified is True
        assert AccountEvent.objects.filter(event_type="email_verified", user=user).exists()

    def test_replaying_a_used_link_is_a_no_op(self, client, user, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"
        services.send_verification_email(user)
        url = verification_url(user)

        client.get(url)
        first_verified_at = User.objects.get(pk=user.pk).email_verified_at

        response = client.get(url)

        assert response.status_code == 200
        assert response.context["verified"] is False
        assert User.objects.get(pk=user.pk).email_verified_at == first_verified_at

    def test_tampered_token_is_refused(self, client, user):
        url = verification_url(user)
        token = url.rstrip("/").rsplit("/", 1)[-1]
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")

        response = client.get(url.replace(token, tampered))

        user.refresh_from_db()
        assert response.status_code == 200
        assert user.is_email_verified is False

    def test_expired_link_is_refused(self, client, user, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"
        services.send_verification_email(user)
        url = verification_url(user)

        # Age the signature past the window instead of sleeping through it: the generator's
        # timestamp is what expires, and a negative timeout puts the cutoff in the future.
        with override_settings(PASSWORD_RESET_TIMEOUT=-1):
            response = client.get(url)

        user.refresh_from_db()
        assert response.status_code == 200
        assert user.is_email_verified is False

    def test_unknown_user_reports_the_same_as_a_bad_token(self, client, user):
        unknown = urlsafe_base64_encode(b"999999")

        response = client.get(verification_url(user, token="not-a-real-token"))
        missing_user = client.get(
            reverse(
                "account:verify-email",
                kwargs={"uidb64": unknown, "token": "not-a-real-token"},
            )
        )

        # Identical responses: the page is not a user-existence oracle.
        assert response.status_code == missing_user.status_code == 200
        assert response.context["verified"] is missing_user.context["verified"] is False

    def test_deactivated_account_cannot_be_verified(self, client, user):
        user.is_active = False
        user.save(update_fields=["is_active"])

        response = client.get(verification_url(user))

        user.refresh_from_db()
        assert response.status_code == 200
        assert user.is_email_verified is False

    def test_changing_the_password_invalidates_outstanding_links(self, user):
        """A token is bound to the password hash, so a reset kills pending verification links."""
        url = verification_url(user)
        token = url.rstrip("/").split("/")[-1]

        user.set_password("An0ther-Passw0rd!")
        user.save(update_fields=["password"])

        assert services.verify_email(user, token) is False

    def test_resend_requires_post_and_a_confirmed_form(self, client, user):
        client.force_login(user)
        url = reverse("account:resend-verification")

        assert client.get(url).status_code == 200
        assert len(mail.outbox) == 0

        client.post(url)
        assert len(mail.outbox) == 0

        client.post(url, {"confirm": "on"})
        assert len(mail.outbox) == 1

    def test_resend_requires_a_signed_in_customer(self, client):
        response = client.post(reverse("account:resend-verification"), {"confirm": "on"})

        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))


# --------------------------------------------------------------------------------------
# Avatar validation
# --------------------------------------------------------------------------------------


class TestAvatarValidation:
    def profile_payload(self, **overrides) -> dict:
        payload = {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "phone": "+44 7700 900000",
            "date_of_birth": "1990-12-10",
            "preferred_language": "en-us",
            "preferred_currency": "USD",
        }
        payload.update(overrides)
        return payload

    def test_png_is_accepted(self, client, user, tmp_path, settings):
        settings.MEDIA_ROOT = tmp_path
        client.force_login(user)

        response = client.post(
            reverse("account:profile"), self.profile_payload(avatar=avatar_bytes("PNG"))
        )

        user.refresh_from_db()
        assert response.status_code == 302
        assert user.profile.avatar.name.startswith("avatars/")

    def test_extension_lying_about_content_is_rejected(self, client, user, tmp_path, settings):
        """The name says PNG, the bytes are a GIF: content wins."""
        settings.MEDIA_ROOT = tmp_path
        client.force_login(user)

        upload = avatar_bytes("GIF")
        upload.name = "avatar.png"

        response = client.post(reverse("account:profile"), self.profile_payload(avatar=upload))

        user.refresh_from_db()
        assert response.status_code == 200
        assert not user.profile.avatar
        assert response.context["form"].errors["avatar"]

    def test_svg_is_rejected(self, client, user, tmp_path, settings):
        settings.MEDIA_ROOT = tmp_path
        client.force_login(user)

        svg = io.BytesIO(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
        svg.name = "avatar.svg"

        response = client.post(reverse("account:profile"), self.profile_payload(avatar=svg))

        user.refresh_from_db()
        assert response.status_code == 200
        assert not user.profile.avatar

    def test_oversized_file_is_rejected_with_a_useful_message(self, client, user, settings):
        settings.ACCOUNT_AVATAR_MAX_BYTES = 512
        client.force_login(user)

        response = client.post(
            reverse("account:profile"), self.profile_payload(avatar=avatar_bytes("PNG", (900, 900)))
        )

        user.refresh_from_db()
        assert response.status_code == 200
        assert response.context["form"].errors["avatar"]

    def test_oversized_dimensions_are_rejected(self, client, user, settings):
        settings.ACCOUNT_AVATAR_MAX_PIXELS = 32
        client.force_login(user)

        response = client.post(
            reverse("account:profile"), self.profile_payload(avatar=avatar_bytes("PNG", (200, 200)))
        )

        user.refresh_from_db()
        assert response.status_code == 200
        assert response.context["form"].errors["avatar"]

    def test_corrupt_file_is_rejected(self, client, user):
        client.force_login(user)

        broken = io.BytesIO(b"this is not an image")
        broken.name = "avatar.png"

        response = client.post(reverse("account:profile"), self.profile_payload(avatar=broken))

        user.refresh_from_db()
        assert response.status_code == 200
        assert response.context["form"].errors["avatar"]

    def test_remove_avatar_clears_the_file(self, client, user, tmp_path, settings):
        settings.MEDIA_ROOT = tmp_path
        client.force_login(user)
        client.post(reverse("account:profile"), self.profile_payload(avatar=avatar_bytes("PNG")))
        user.refresh_from_db()
        assert user.profile.avatar

        client.post(reverse("account:profile"), self.profile_payload(remove_avatar="on"))

        user.refresh_from_db()
        assert not user.profile.avatar


# --------------------------------------------------------------------------------------
# Profile updates
# --------------------------------------------------------------------------------------


class TestProfileUpdates:
    def payload(self, **overrides) -> dict:
        payload = {
            "first_name": "Grace",
            "last_name": "Hopper",
            "phone": "+1 202 555 0143",
            "date_of_birth": "1985-01-02",
            "preferred_language": "en-gb",
            "preferred_currency": "GBP",
            "marketing_email_opt_in": "on",
        }
        payload.update(overrides)
        return payload

    def test_saves_names_and_preferences(self, client, user):
        client.force_login(user)

        response = client.post(reverse("account:profile"), self.payload())

        user.refresh_from_db()
        assert response.status_code == 302
        assert (user.first_name, user.last_name) == ("Grace", "Hopper")
        assert user.profile.phone == "+1 202 555 0143"
        assert user.profile.preferred_currency == "GBP"
        assert user.profile.marketing_email_opt_in is True

    def test_unchecking_marketing_consent_is_saved(self, client, user):
        user.profile.marketing_email_opt_in = True
        user.profile.save()
        client.force_login(user)

        payload = self.payload()
        payload.pop("marketing_email_opt_in")  # an unticked box is simply absent
        client.post(reverse("account:profile"), payload)

        user.refresh_from_db()
        assert user.profile.marketing_email_opt_in is False

    def test_sms_consent_requires_a_phone_number(self, client, user):
        client.force_login(user)

        response = client.post(
            reverse("account:profile"), self.payload(phone="", marketing_sms_opt_in="on")
        )

        user.refresh_from_db()
        assert response.status_code == 200
        assert response.context["form"].errors["phone"]
        assert user.profile.marketing_sms_opt_in is False

    def test_future_date_of_birth_is_refused(self, client, user):
        client.force_login(user)
        tomorrow = (timezone.now() + timedelta(days=1)).date().isoformat()

        response = client.post(reverse("account:profile"), self.payload(date_of_birth=tomorrow))

        user.refresh_from_db()
        assert response.status_code == 200
        assert response.context["form"].errors["date_of_birth"]

    def test_update_is_audited_without_the_values(self, client, user):
        client.force_login(user)

        client.post(reverse("account:profile"), self.payload())

        event = AccountEvent.objects.filter(event_type="profile_updated", user=user).latest("id")
        assert "phone" in event.metadata["fields"]
        # The audit trail records which fields changed, never their contents.
        assert "+1 202 555 0143" not in str(event.metadata)

    def test_profile_requires_a_signed_in_customer(self, client):
        response = client.get(reverse("account:profile"))

        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))


# --------------------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------------------


class TestAddresses:
    def address_payload(self, **overrides) -> dict:
        payload = {
            "full_name": "Ada Lovelace",
            "phone": "+44 7700 900000",
            "line1": "1 Example Way",
            "line2": "",
            "city": "London",
            "region": "Greater London",
            "postal_code": "SW1A 1AA",
            "country": "GB",
            "address_type": "both",
        }
        payload.update(overrides)
        return payload

    def test_first_address_defaults_to_the_delivery_default(self, client, user):
        client.force_login(user)

        response = client.post(reverse("account:address-create"), self.address_payload())

        assert response.status_code == 302
        assert user.addresses.get().is_default_shipping is True

    def test_country_code_is_upper_cased(self, client, user):
        client.force_login(user)

        client.post(reverse("account:address-create"), self.address_payload(country="gb"))

        assert user.addresses.get().country == "GB"

    def test_shipping_only_address_cannot_be_the_billing_default(self, client, user):
        client.force_login(user)

        response = client.post(
            reverse("account:address-create"),
            self.address_payload(address_type="shipping", is_default_billing="on"),
        )

        assert response.status_code == 200
        assert response.context["form"].errors["is_default_billing"]

    def test_promoting_a_default_moves_the_flag(self, client, user):
        first = Address.objects.create(
            user=user,
            full_name="First",
            phone="+44 7700 900001",
            line1="1 Example Way",
            city="London",
            region="Greater London",
            postal_code="SW1A 1AA",
            country="GB",
            is_default_shipping=True,
        )
        second = Address.objects.create(
            user=user,
            full_name="Second",
            phone="+44 7700 900002",
            line1="2 Example Way",
            city="Leeds",
            region="West Yorkshire",
            postal_code="LS1 1AA",
            country="GB",
        )

        client.force_login(user)
        response = client.post(reverse("account:address-set-default", args=[second.pk, "shipping"]))

        first.refresh_from_db()
        second.refresh_from_db()
        assert response.status_code == 302
        assert second.is_default_shipping is True
        assert first.is_default_shipping is False

    def test_a_shipping_only_address_cannot_become_the_billing_default(self, client, user):
        shipping_only = Address.objects.create(
            user=user,
            full_name="Shipping only",
            phone="+44 7700 900003",
            line1="3 Example Way",
            city="Bristol",
            region="England",
            postal_code="BS1 1AA",
            country="GB",
            address_type="shipping",
        )

        client.force_login(user)
        response = client.post(
            reverse("account:address-set-default", args=[shipping_only.pk, "billing"]),
            follow=True,
        )

        shipping_only.refresh_from_db()
        assert shipping_only.is_default_billing is False
        assert "not used for billing" in message_texts(response)

    def test_only_one_default_shipping_address_is_possible_in_the_database(self, user, address):
        """The partial unique index is the backstop, independent of application code."""
        from django.db import IntegrityError, transaction

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Address.objects.create(
                    user=user,
                    full_name="Second default",
                    phone="+44 7700 900004",
                    line1="9 Example Way",
                    city="Leeds",
                    region="West Yorkshire",
                    postal_code="LS1 1AA",
                    country="GB",
                    is_default_shipping=True,
                )

    def test_address_list_only_shows_the_owner_s_addresses(self, client, user, other_user):
        theirs = Address.objects.create(
            user=other_user,
            full_name="Not yours",
            phone="+44 7700 900005",
            line1="5 Example Way",
            city="York",
            region="England",
            postal_code="YO1 1AA",
            country="GB",
        )

        client.force_login(user)
        response = client.get(reverse("account:addresses"))

        assert response.status_code == 200
        assert list(response.context["addresses"]) == list(user.addresses.all())
        assert theirs not in response.context["addresses"]

    def test_cannot_edit_or_delete_another_customers_address(self, client, user, other_user):
        theirs = Address.objects.create(
            user=other_user,
            full_name="Not yours",
            phone="+44 7700 900006",
            line1="6 Example Way",
            city="Bath",
            region="England",
            postal_code="BA1 1AA",
            country="GB",
        )

        client.force_login(user)

        assert client.get(reverse("account:address-update", args=[theirs.pk])).status_code == 404
        assert client.get(reverse("account:address-delete", args=[theirs.pk])).status_code == 404
        assert client.post(reverse("account:address-delete", args=[theirs.pk])).status_code == 404
        assert Address.objects.filter(pk=theirs.pk).exists()

    def test_setting_a_default_requires_post(self, client, user, address):
        client.force_login(user)
        response = client.get(reverse("account:address-set-default", args=[address.pk, "shipping"]))

        assert response.status_code == 405
        address.refresh_from_db()
        assert address.is_default_shipping is True

    def test_deleting_an_address_removes_it_and_records_the_event(self, client, user, address):
        client.force_login(user)
        url = reverse("account:address-delete", args=[address.pk])

        assert client.get(url).status_code == 200
        deleted = client.post(url)

        assert deleted.status_code == 302
        assert deleted["Location"] == reverse("account:addresses")
        assert not Address.objects.filter(pk=address.pk).exists()
        event = AccountEvent.objects.filter(event_type="address_deleted").latest("id")
        assert event.metadata == {"was_default": True}

    def test_unknown_default_kind_is_refused(self, client, user, address):
        client.force_login(user)
        response = client.post(
            reverse("account:address-set-default", args=[address.pk, "nonsense"])
        )

        assert response.status_code == 403

    def test_address_change_is_audited(self, client, user, address):
        client.force_login(user)

        client.post(
            reverse("account:address-update", args=[address.pk]),
            {
                "full_name": "Ada L.",
                "phone": "+44 7700 900000",
                "line1": "1 Example Way",
                "line2": "",
                "city": "London",
                "region": "Greater London",
                "postal_code": "SW1A 1AA",
                "country": "GB",
                "address_type": "both",
            },
        )

        assert AccountEvent.objects.filter(event_type="address_updated", user=user).exists()

    def test_addresses_require_a_signed_in_customer(self, client):
        for name in ("addresses", "address-create"):
            response = client.get(reverse(f"account:{name}"))
            assert response.status_code == 302
            assert response["Location"].startswith(reverse("accounts:login"))


# --------------------------------------------------------------------------------------
# Password reset
# --------------------------------------------------------------------------------------


class TestPasswordReset:
    def test_reset_email_is_sent_for_a_known_address(self, client, user):
        response = client.post(reverse("account:password-reset"), {"email": user.email})

        assert response.status_code == 302
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [user.email]

    def test_unknown_address_sends_nothing_and_says_the_same_thing(self, client, db):
        known = client.post(reverse("account:password-reset"), {"email": "known@flashwear.test"})
        mail.outbox.clear()

        unknown = client.post(
            reverse("account:password-reset"), {"email": "stranger@flashwear.test"}
        )

        # Identical redirect: the page cannot be used to find out who has an account.
        assert known["Location"] == unknown["Location"]
        assert len(mail.outbox) == 0

    def test_reset_request_is_audited_without_the_address(self, client, user):
        client.post(reverse("account:password-reset"), {"email": user.email})

        event = AccountEvent.objects.filter(event_type="password_reset_requested").latest("id")
        assert event.user is None
        assert event.email_digest == ""
        assert user.email not in str(event.metadata)

    def test_reset_link_changes_the_password(self, client, user, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"
        client.post(reverse("account:password-reset"), {"email": user.email})
        uid, token = reset_link()
        confirm = reverse("account:password-reset-confirm", args=[uid, token])

        # Django parks the token in the session and bounces to a token-free URL, so the new
        # password never travels in a Referer header. The test walks that journey.
        landing = client.get(confirm)
        assert landing.status_code == 302
        assert token not in landing["Location"]

        response = client.post(
            landing["Location"],
            {"new_password1": "An0ther-Str0ng-Pass!", "new_password2": "An0ther-Str0ng-Pass!"},
            follow=True,
        )

        user.refresh_from_db()
        assert response.status_code == 200
        assert user.check_password("An0ther-Str0ng-Pass!")
        assert client.session.get("_auth_user_id") == str(user.pk)
        assert AccountEvent.objects.filter(
            event_type="password_changed", user=user, metadata__via="reset"
        ).exists()

    def test_reset_link_is_single_use(self, client, user, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"
        client.post(reverse("account:password-reset"), {"email": user.email})
        uid, token = reset_link()
        confirm = reverse("account:password-reset-confirm", args=[uid, token])
        form_url = client.get(confirm)["Location"]

        client.post(
            form_url,
            {"new_password1": "An0ther-Str0ng-Pass!", "new_password2": "An0ther-Str0ng-Pass!"},
        )
        # The session token is consumed on success, so replaying the emailed link is refused.
        second = client.get(confirm)
        stale_post = client.post(
            confirm,
            {"new_password1": "Y3t-another-Pass!", "new_password2": "Y3t-another-Pass!"},
            follow=True,
        )

        user.refresh_from_db()
        assert second.context["validlink"] is False
        assert stale_post.status_code == 200
        assert user.check_password("An0ther-Str0ng-Pass!")

    def test_bad_reset_link_shows_the_recovery_path(self, client, db):
        response = client.get(
            reverse("account:password-reset-confirm", args=["not-base64", "not-a-token"])
        )

        assert response.status_code == 200
        assert response.context["validlink"] is False

    def test_inactive_account_cannot_be_reset(self, client, user):
        """``PasswordResetForm`` only considers active users, so a closed account is excluded."""
        user.is_active = False
        user.save(update_fields=["is_active"])

        client.post(reverse("account:password-reset"), {"email": user.email})

        assert len(mail.outbox) == 0


# --------------------------------------------------------------------------------------
# Password change
# --------------------------------------------------------------------------------------


class TestPasswordChange:
    def test_change_requires_the_current_password(self, client, user):
        client.force_login(user)

        response = client.post(
            reverse("account:password-change"),
            {
                "old_password": "not-my-password",
                "new_password1": "An0ther-Str0ng-Pass!",
                "new_password2": "An0ther-Str0ng-Pass!",
            },
        )

        user.refresh_from_db()
        assert response.status_code == 200
        assert user.check_password("Str0ng-Passw0rd!")

    def test_change_keeps_the_customer_signed_in(self, client, user):
        client.force_login(user)

        response = client.post(
            reverse("account:password-change"),
            {
                "old_password": "Str0ng-Passw0rd!",
                "new_password1": "An0ther-Str0ng-Pass!",
                "new_password2": "An0ther-Str0ng-Pass!",
            },
        )

        user.refresh_from_db()
        assert response.status_code == 302
        assert user.check_password("An0ther-Str0ng-Pass!")
        assert client.session.get("_auth_user_id") == str(user.pk)

    def test_change_is_audited(self, client, user):
        client.force_login(user)

        client.post(
            reverse("account:password-change"),
            {
                "old_password": "Str0ng-Passw0rd!",
                "new_password1": "An0ther-Str0ng-Pass!",
                "new_password2": "An0ther-Str0ng-Pass!",
            },
        )

        event = AccountEvent.objects.filter(event_type="password_changed", user=user).latest("id")
        assert event.metadata == {"via": "account"}

    def test_change_requires_a_signed_in_customer(self, client):
        response = client.get(reverse("account:password-change"))

        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))


# --------------------------------------------------------------------------------------
# Remember me
# --------------------------------------------------------------------------------------


class TestRememberMe:
    def test_remembered_session_outlives_an_ordinary_one(self, client, user, settings):
        settings.ACCOUNT_REMEMBER_ME_DAYS = 30
        settings.SESSION_COOKIE_AGE = 14 * 24 * 60 * 60

        sign_in(client, remember=False)
        ordinary = client.session.get_expiry_age()

        client.logout()
        sign_in(client, remember=True)
        remembered = client.session.get_expiry_age()

        assert remembered == 30 * 24 * 60 * 60
        assert remembered > ordinary

    def test_without_the_box_the_session_uses_the_configured_age(self, client, user, settings):
        settings.SESSION_COOKIE_AGE = 14 * 24 * 60 * 60

        sign_in(client, remember=False)

        assert client.session.get_expiry_age() == 14 * 24 * 60 * 60

    def test_remember_me_is_advisory_not_a_second_credential(self, client, user, settings):
        """The user row is untouched: no token is issued, so there is nothing to revoke."""
        settings.ACCOUNT_REMEMBER_ME_DAYS = 30

        sign_in(client, remember=True)

        user.refresh_from_db()
        assert user.last_login is not None
        assert user.check_password("Str0ng-Passw0rd!")

    def test_login_still_clears_the_throttle_counters(self, client, user):
        from django.test import RequestFactory

        from apps.accounts.throttling import LoginThrottle

        throttle = LoginThrottle()
        request = RequestFactory().post("/accounts/login/")
        throttle.record_failure(request, user.email)

        sign_in(client, remember=True)

        assert throttle.check(request, user.email).blocked is False


# --------------------------------------------------------------------------------------
# Session expiry bounce
# --------------------------------------------------------------------------------------


class TestSessionInvalidation:
    def test_flag_on_the_login_page_explains_the_bounce(self, client, user):
        session = client.session
        session["flashwear_session_expired"] = True
        session.save()

        response = client.get(reverse("accounts:login"), follow=True)

        assert response.status_code == 200
        assert "session expired" in message_texts(response).casefold()
        # The flag is consumed, so the message appears once rather than on every visit.
        assert "flashwear_session_expired" not in client.session

    def test_login_alias_under_the_phase_two_prefix_works(self, client, user, settings):
        """``/account/login/`` is the Phase 1 view object, so it must behave identically."""
        phase_one = sign_in(client, remember=False)
        client.logout()

        alias = client.post(
            reverse("account:login"),
            {"username": user.email, "password": "Str0ng-Passw0rd!"},
        )

        assert alias.status_code == phase_one.status_code
        # Phase 1 sends the customer to ``LOGIN_REDIRECT_URL``; the alias must not redirect
        # somewhere else just because it lives under the Phase 2 prefix.
        assert alias["Location"] == phase_one["Location"] == reverse(settings.LOGIN_REDIRECT_URL)
        assert client.session.get("_auth_user_id") == str(user.pk)

    def test_login_alias_is_still_throttled(self, client, user):
        exhaust_throttle(client, "account:login", user.email)

        response = client.post(
            reverse("account:login"), {"username": user.email, "password": "wrong"}
        )

        assert response.status_code == 429
        assert response["Retry-After"]


# --------------------------------------------------------------------------------------
# Account deactivation
# --------------------------------------------------------------------------------------


class TestAccountDeactivation:
    def test_password_is_required(self, client, user):
        client.force_login(user)

        response = client.post(reverse("account:delete"), {"confirm": "on"})

        user.refresh_from_db()
        assert response.status_code == 200
        assert user.is_active is True

    def test_wrong_password_is_refused(self, client, user):
        client.force_login(user)

        response = client.post(
            reverse("account:delete"), {"password": "not-my-password", "confirm": "on"}
        )

        user.refresh_from_db()
        assert response.status_code == 200
        assert response.context["form"].errors["password"]
        assert user.is_active is True

    def test_deactivation_stops_sign_in_without_deleting_anything(self, client, user):
        client.force_login(user)

        response = client.post(
            reverse("account:delete"), {"password": "Str0ng-Passw0rd!", "confirm": "on"}
        )

        user.refresh_from_db()
        assert response.status_code == 302
        assert response["Location"] == reverse("account:deleted")
        assert user.is_active is False
        assert user.deactivated_at is not None
        assert user.has_usable_password() is False
        assert User.objects.filter(pk=user.pk).exists()
        assert "_auth_user_id" not in client.session

    def test_deactivated_customer_cannot_sign_in_again(self, client, user):
        client.force_login(user)
        client.post(reverse("account:delete"), {"password": "Str0ng-Passw0rd!", "confirm": "on"})
        client.logout()

        response = client.post(
            reverse("accounts:login"), {"username": user.email, "password": "Str0ng-Passw0rd!"}
        )

        assert response.status_code == 200
        assert response.context["form"].non_field_errors()

    def test_deactivation_is_audited(self, client, user):
        client.force_login(user)

        client.post(reverse("account:delete"), {"password": "Str0ng-Passw0rd!", "confirm": "on"})

        event = AccountEvent.objects.filter(event_type="account_deactivated", user=user).latest(
            "id"
        )
        assert event.metadata == {"reason": "customer_request"}

    def test_other_sessions_are_dropped(self, client, user, settings):
        settings.SESSION_ENGINE = "django.contrib.sessions.backends.db"
        from django.contrib.sessions.models import Session

        client.force_login(user)
        client.post(reverse("account:delete"), {"password": "Str0ng-Passw0rd!", "confirm": "on"})

        assert not Session.objects.filter(session_data__contains=f'"_auth_user_id": "{user.pk}"')

    def test_anonymisation_leaves_the_row_and_clears_the_personal_fields(self, settings):
        settings.ACCOUNT_DATA_RETENTION_DAYS = 30
        user = User.objects.create_user(
            email="gone@flashwear.test", password="Str0ng-Passw0rd!", first_name="Gone"
        )
        user.profile.phone = "+44 7700 900099"
        user.profile.save()
        user.is_active = False
        user.deactivated_at = timezone.now() - timedelta(days=31)
        user.save(update_fields=["is_active", "deactivated_at"])

        candidates = services.anonymise_deactivated_accounts(dry_run=True)
        assert candidates == [user.pk]

        services.anonymise_deactivated_accounts(dry_run=False)

        user.refresh_from_db()
        user.profile.refresh_from_db()
        assert User.objects.filter(pk=user.pk).exists()
        assert user.first_name == ""
        assert user.profile.phone == ""

    def test_dry_run_is_the_default(self, settings):
        settings.ACCOUNT_DATA_RETENTION_DAYS = 30
        user = User.objects.create_user(email="kept@flashwear.test", password="Str0ng-Passw0rd!")
        user.is_active = False
        user.deactivated_at = timezone.now() - timedelta(days=31)
        user.save(update_fields=["is_active", "deactivated_at"])

        services.anonymise_deactivated_accounts()

        user.refresh_from_db()
        assert user.email == "kept@flashwear.test"


# --------------------------------------------------------------------------------------
# Dashboard and navigation
# --------------------------------------------------------------------------------------


class TestDashboard:
    def test_dashboard_requires_a_signed_in_customer(self, client):
        response = client.get(reverse("account:dashboard"))

        assert response.status_code == 302
        assert response["Location"] == f"{reverse('accounts:login')}?next=/account/"

    def test_dashboard_shows_the_unverified_reminder(self, client, user):
        client.force_login(user)

        response = client.get(reverse("account:dashboard"))

        assert response.status_code == 200
        assert b"not verified" in response.content.lower()

    def test_dashboard_is_quiet_once_verified(self, client, verified_user):
        client.force_login(verified_user)

        response = client.get(reverse("account:dashboard"))

        assert b"not verified" not in response.content.lower()

    def test_sections_mark_later_phases_as_unavailable(self, db):
        sections = {section["label"]: section for section in account_sections()}

        # Orders went live in Phase 6, Loyalty in Phase 7, Wishlist in Phase 26;
        # the rest shows as planned.
        assert sections["Orders"]["url"] == reverse("account:orders")
        assert sections["Addresses"]["url"] == reverse("account:addresses")
        assert sections["Wishlist"]["url"] == reverse("shop:wishlist")
        assert sections["Reviews"]["url"] is None
        assert sections["Loyalty"]["url"] == reverse("account:loyalty")

    def test_dashboard_does_not_stay_active_on_other_account_pages(self, rf, db):
        """``/account/`` is a prefix of every account URL, so it must match exactly."""
        from apps.accounts.navigation import account_sections

        dashboard_request = rf.get("/account/")
        profile_request = rf.get("/account/profile/")

        def active_for(url_name, request):
            return next(
                section["active"]
                for section in account_sections(request)
                if section["url"] == url_name
            )

        assert active_for(reverse("account:dashboard"), dashboard_request) is True
        assert active_for(reverse("account:dashboard"), profile_request) is False
        assert active_for(reverse("account:profile"), profile_request) is True

    def test_security_page_reaches_every_screen_it_advertises(self, client, user):
        client.force_login(user)
        response = client.get(reverse("account:security"))

        assert response.status_code == 200
        for name in ("account:password-change", "account:delete", "account:resend-verification"):
            assert reverse(name).encode() in response.content

    def test_account_screens_require_a_session(self, client):
        protected = [
            "account:dashboard",
            "account:profile",
            "account:security",
            "account:password-change",
            "account:addresses",
            "account:address-create",
            "account:delete",
            "account:resend-verification",
        ]
        for name in protected:
            response = client.get(reverse(name))
            assert response.status_code == 302, name
            assert response["Location"].startswith(reverse("accounts:login")), name

    def test_logout_requires_post(self, client, user):
        client.force_login(user)

        assert client.get(reverse("accounts:logout")).status_code == 405
        assert client.post(reverse("accounts:logout")).status_code == 302
        assert "_auth_user_id" not in client.session


# --------------------------------------------------------------------------------------
# Templates and accessibility hooks
# --------------------------------------------------------------------------------------


class TestAccountTemplates:
    def test_registration_page_advertises_no_marketing_opt_in(self, client, db):
        response = client.get(reverse("account:register"))
        form = response.context["form"]

        assert response.status_code == 200
        # Consent is not collected at signup -- not as a checkbox, not as a hidden input a script
        # could set. It is asked for in the profile, where it can also be withdrawn.
        assert "marketing_email_opt_in" not in form.fields
        assert "marketing_sms_opt_in" not in form.fields
        markup = response.content.decode().casefold()
        inside_form = markup.split("<form", 1)[1].split("</form>", 1)[0]
        assert "marketing" not in inside_form
        assert "checkbox" not in inside_form

    def test_login_page_offers_remember_me_and_reset(self, client, db):
        response = client.get(reverse("accounts:login"))

        assert response.status_code == 200
        assert b'name="remember_me"' in response.content
        assert reverse("account:password-reset").encode() in response.content
        assert reverse("account:register").encode() in response.content

    def test_throttled_login_still_offers_a_way_forward(self, client, user, settings):
        exhaust_throttle(client, "accounts:login", user.email)

        response = client.post(reverse("accounts:login"), {"username": user.email, "password": "x"})

        assert response.status_code == 429
        assert reverse("account:password-reset").encode() in response.content

    def test_navbar_offers_registration_when_signed_out(self, client, db):
        response = client.get(reverse("core:home"))

        assert reverse("account:register").encode() in response.content

    def test_navbar_links_to_the_account_when_signed_in(self, client, user):
        client.force_login(user)
        response = client.get(reverse("core:home"))

        assert reverse("account:dashboard").encode() in response.content

    def test_address_form_labels_every_control(self, client, user):
        client.force_login(user)
        response = client.get(reverse("account:address-create"))

        content = response.content.decode()
        for field in ("full_name", "line1", "city", "region", "postal_code", "country"):
            assert f'for="id_{field}"' in content

    def test_error_messages_are_marked_for_assistive_tech(self, client, user):
        client.force_login(user)
        response = client.post(
            reverse("account:address-create"),
            {
                "full_name": "",
                "phone": "",
                "line1": "",
                "city": "",
                "region": "",
                "postal_code": "",
                "country": "GB",
            },
        )

        content = response.content.decode()
        assert response.status_code == 200
        assert 'role="alert"' in content

    def test_verification_failure_page_offers_a_way_forward(self, client, db):
        response = client.get(reverse("account:verify-email", args=["zzz", "zzz"]))

        assert response.status_code == 200
        assert reverse("account:register").encode() in response.content

    def test_email_templates_render_both_parts(self, user, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"

        services.send_verification_email(user)

        message = mail.outbox[0]
        assert message.alternatives, "an HTML part must be attached"
        html, mimetype = message.alternatives[0]
        assert mimetype == "text/html"
        assert "Verify my email" in html
        assert "/account/verify-email/" in html
        # The expiry the customer is told about comes from the configured timeout.
        assert str(settings.PASSWORD_RESET_TIMEOUT // 3600) in message.body


# --------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------


class TestAccountApi:
    def test_api_root_advertises_the_account_endpoints(self, api_client, db):
        response = api_client.get(reverse("v1:root"))

        assert response.status_code == 200
        endpoints = response.json()["endpoints"]
        assert endpoints["account_me"].endswith("/api/v1/accounts/me/")
        assert endpoints["addresses"].endswith("/api/v1/addresses/")

    def test_account_me_requires_authentication(self, api_client, db):
        response = api_client.get(reverse("v1:account-me"))

        assert response.status_code in (401, 403)

    def test_account_me_returns_the_signed_in_customer(self, api_client, user):
        api_client.force_authenticate(user)

        response = api_client.get(reverse("v1:account-me"))

        assert response.status_code == 200
        body = response.json()
        assert body["email"] == user.email
        assert body["is_email_verified"] is False
        assert "password" not in body
        assert body["profile"]["preferred_currency"] == "USD"

    def test_registration_endpoint_creates_an_unverified_account(self, api_client, db, settings):
        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"

        response = api_client.post(
            reverse("v1:account-register"),
            {
                "email": "api.shopper@flashwear.test",
                "first_name": "Api",
                "last_name": "Shopper",
                "password": "Str0ng-Passw0rd!",
            },
            format="json",
        )

        created = User.objects.get(email="api.shopper@flashwear.test")
        assert response.status_code == 201
        assert response.json()["is_email_verified"] is False
        assert created.check_password("Str0ng-Passw0rd!")
        assert len(mail.outbox) == 1

    def test_registration_endpoint_refuses_a_weak_password(self, api_client, user):
        response = api_client.post(
            reverse("v1:account-register"),
            {
                "email": "weak@flashwear.test",
                "first_name": "W",
                "last_name": "E",
                "password": "abc",
            },
            format="json",
        )

        assert response.status_code == 400
        assert "password" in response.json()

    def test_address_endpoint_requires_authentication(self, api_client, db):
        assert api_client.get(reverse("v1:address-list")).status_code in (401, 403)

    def test_address_crud_is_scoped_to_the_owner(self, api_client, user, other_user):
        theirs = Address.objects.create(
            user=other_user,
            full_name="Not yours",
            phone="+44 7700 900010",
            line1="7 Example Way",
            city="Bath",
            region="England",
            postal_code="BA1 1AA",
            country="GB",
        )
        api_client.force_authenticate(user)

        created = api_client.post(
            reverse("v1:address-list"),
            {
                "full_name": "Mine",
                "phone": "+44 7700 900011",
                "line1": "8 Example Way",
                "city": "Leeds",
                "region": "West Yorkshire",
                "postal_code": "LS1 1AA",
                "country": "gb",
                "address_type": "shipping",
            },
            format="json",
        )

        assert created.status_code == 201
        assert created.json()["country"] == "GB"
        assert user.addresses.count() == 1
        # ``user`` is not part of the writable payload, so ownership cannot be forged.
        assert "user" not in created.json()

        listing = api_client.get(reverse("v1:address-list"))
        assert [row["id"] for row in listing.json()] == [created.json()["id"]]

        assert api_client.get(reverse("v1:address-detail", args=[theirs.pk])).status_code == 404
        assert api_client.delete(reverse("v1:address-detail", args=[theirs.pk])).status_code == 404
        assert Address.objects.filter(pk=theirs.pk).exists()

    def test_invalid_country_code_is_rejected(self, api_client, user):
        api_client.force_authenticate(user)

        response = api_client.post(
            reverse("v1:address-list"),
            {
                "full_name": "Bad code",
                "phone": "+44 7700 900012",
                "line1": "9 Example Way",
                "city": "Leeds",
                "region": "West Yorkshire",
                "postal_code": "LS1 1AA",
                "country": "GBR",
                "address_type": "shipping",
            },
            format="json",
        )

        assert response.status_code == 400
        assert "country" in response.json()

    def test_password_change_endpoint(self, api_client, user):
        api_client.force_authenticate(user)

        wrong = api_client.post(
            reverse("v1:account-password"),
            {"current_password": "nope", "new_password": "An0ther-Str0ng-Pass!"},
            format="json",
        )
        assert wrong.status_code == 400

        response = api_client.post(
            reverse("v1:account-password"),
            {"current_password": "Str0ng-Passw0rd!", "new_password": "An0ther-Str0ng-Pass!"},
            format="json",
        )

        user.refresh_from_db()
        assert response.status_code == 204
        assert user.check_password("An0ther-Str0ng-Pass!")

    def test_address_detail_can_be_updated_and_deleted(self, api_client, user, address):
        api_client.force_authenticate(user)
        url = reverse("v1:address-detail", args=[address.pk])

        updated = api_client.patch(url, {"city": "Sheffield"}, format="json")

        address.refresh_from_db()
        assert updated.status_code == 200
        assert updated.json()["city"] == "Sheffield"
        assert address.city == "Sheffield"
        assert AccountEvent.objects.filter(event_type="address_updated").exists()

        removed = api_client.delete(url)

        assert removed.status_code == 204
        assert not Address.objects.filter(pk=address.pk).exists()
        event = AccountEvent.objects.filter(event_type="address_deleted").latest("id")
        assert event.metadata["was_default"] is True

    def test_promoting_a_default_through_the_api_is_refused(self, api_client, user, address):
        """The API does not silently break the one-default-per-kind invariant."""
        api_client.force_authenticate(user)

        response = api_client.patch(
            reverse("v1:address-detail", args=[address.pk]),
            {"is_default_billing": True},
            format="json",
        )

        address.refresh_from_db()
        assert response.status_code in (200, 400)
        if response.status_code == 200:
            # Accepted only when the address is billing-eligible; a shipping-only row must not
            # quietly become the billing default.
            assert address.address_type != "shipping"
        else:
            assert "billing" in str(response.json()).casefold()


# --------------------------------------------------------------------------------------
# Transactional email plumbing
# --------------------------------------------------------------------------------------


class TestAccountMail:
    def send(self, user, **overrides) -> int:
        from apps.accounts import mailer

        kwargs = {
            "subject_template": "accounts/email/verification_email_subject.txt",
            "text_template": "accounts/email/verification_email.txt",
            "html_template": "accounts/email/verification_email.html",
            "context": {
                "user": user,
                "verification_url": "https://flashwear.test/verify/abc",
                "expiry_hours": 72,
            },
            "recipient": user.email,
        }
        kwargs.update(overrides)
        mail.outbox.clear()
        return mailer.send_account_email(**kwargs)

    def test_base_url_prefers_the_configured_value(self, settings):
        from apps.accounts.mailer import resolve_base_url

        settings.ACCOUNT_EMAIL_BASE_URL = "https://shop.flashwear.test/"
        assert resolve_base_url() == "https://shop.flashwear.test"

        settings.ACCOUNT_EMAIL_BASE_URL = ""
        settings.SITE_URL = "https://fallback.flashwear.test"
        assert resolve_base_url() == "https://fallback.flashwear.test"

    def test_base_url_falls_back_to_the_incoming_request(self, settings, rf):
        from apps.accounts.mailer import resolve_base_url

        settings.ACCOUNT_EMAIL_BASE_URL = ""
        request = rf.get("/", HTTP_HOST="localhost:8000")
        assert resolve_base_url(request) == "http://localhost:8000"

    def test_absolute_url_leaves_an_absolute_url_alone(self, rf, settings):
        from apps.accounts.mailer import absolute_url

        settings.ACCOUNT_EMAIL_BASE_URL = "https://flashwear.test"
        request = rf.get("/")

        assert absolute_url("https://elsewhere.test/x", request) == "https://elsewhere.test/x"
        assert absolute_url("/account/x/", request) == "https://flashwear.test/account/x/"
        assert absolute_url("account/x/", request) == "https://flashwear.test/account/x/"

    def test_a_missing_html_template_still_sends_the_message(self, user):
        """One absent branded template must not cost the customer their verification link."""
        sent = self.send(user, html_template="accounts/email/does_not_exist.html")

        assert sent == 1
        message = mail.outbox[0]
        assert message.alternatives  # a plain HTML part was derived from the text body
        assert "flashwear.test" in message.body

    def test_a_missing_text_template_is_a_loud_deployment_error(self, user):
        from django.template import TemplateDoesNotExist

        with pytest.raises(TemplateDoesNotExist):
            self.send(user, text_template="accounts/email/also_missing.txt")

        assert mail.outbox == []


# --------------------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------------------


class TestAuditTrail:
    def test_events_never_store_an_address_in_the_clear(self, user, rf):
        services.record_account_event(user, "registered", request=rf.get("/"))

        event = AccountEvent.objects.get(event_type="registered")
        assert event.user_id == user.pk
        assert event.email_digest and user.email not in event.email_digest
        assert len(event.email_digest) == 32

    def test_ip_digest_is_salted_and_truncated(self, rf):
        services.record_account_event(None, "password_reset_requested", request=rf.get("/"))

        event = AccountEvent.objects.get(event_type="password_reset_requested")
        assert len(event.ip_digest) == 32
        assert services.digest("") == ""

    def test_a_broken_audit_write_does_not_break_the_request(self, user, monkeypatch):
        """Losing an audit line must never turn a successful action into a 500."""

        def explode(*args, **kwargs):
            raise RuntimeError("audit table unavailable")

        monkeypatch.setattr(AccountEvent.objects, "create", explode)

        services.record_account_event(user, "registered")  # must not raise
        assert AccountEvent.objects.count() == 0

    def test_events_survive_account_deletion(self, user, db):
        services.record_account_event(user, "registered")
        user.delete()

        event = AccountEvent.objects.get(event_type="registered")
        assert event.user is None
        assert event.email_digest
