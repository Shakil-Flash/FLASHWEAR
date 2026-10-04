"""Tests for the custom ``accounts.User`` model, manager, auth backend and auth views."""

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.core.cache import cache
from django.db import IntegrityError
from django.test import RequestFactory
from django.urls import resolve, reverse

from apps.accounts.throttling import LoginThrottle, _client_ip

pytestmark = pytest.mark.django_db

User = get_user_model()


class TestUserModel:
    """Model-level behaviour of the FLASHWEAR user."""

    def test_username_field_is_email(self):
        assert User.USERNAME_FIELD == "email"
        assert User.REQUIRED_FIELDS == []

    def test_str_is_email(self, user):
        assert str(user) == user.email

    def test_get_full_name_falls_back_to_email(self, user):
        assert user.get_full_name() == user.email
        assert user.get_short_name() == user.email

    def test_get_full_name_uses_names(self, db):
        person = User.objects.create_user(
            email="named@flashwear.test",
            password="Str0ng-Passw0rd!",
            first_name="Ada",
            last_name="Lovelace",
        )
        assert person.get_full_name() == "Ada Lovelace"
        assert person.get_short_name() == "Ada"

    def test_email_is_unique(self, db):
        User.objects.create_user(email="dupe@flashwear.test", password="Str0ng-Passw0rd!")
        with pytest.raises(IntegrityError):
            User.objects.create_user(email="dupe@flashwear.test", password="Str0ng-Passw0rd!")

    def test_email_lookup_is_case_insensitive(self, db):
        User.objects.create_user(email="Case@Flashwear.test", password="Str0ng-Passw0rd!")
        assert User.objects.filter(email__iexact="case@flashwear.test").exists()

    def test_email_is_stored_lowercase(self, db):
        """Case-insensitive sign-in only works if the stored value is canonical."""
        person = User.objects.create_user(
            email="  Mixed.Case@Flashwear.TEST ", password="Str0ng-Passw0rd!"
        )
        assert person.email == "mixed.case@flashwear.test"

    def test_lowercase_email_is_normalized_on_save(self, db, user):
        user.email = "Renamed@Flashwear.TEST"
        user.save()
        user.refresh_from_db()
        assert user.email == "renamed@flashwear.test"

    def test_duplicate_email_in_different_case_is_rejected(self, db):
        User.objects.create_user(email="taken@flashwear.test", password="Str0ng-Passw0rd!")
        with pytest.raises(IntegrityError):
            User.objects.create_user(email="TAKEN@flashwear.test", password="Str0ng-Passw0rd!")

    def test_timestamps_are_set(self, user):
        assert user.date_joined is not None
        assert user.updated_at is not None

    def test_updated_at_refreshes_on_save(self, user):
        before = user.updated_at
        user.first_name = "Refreshed"
        user.save()
        user.refresh_from_db()
        # SQLite truncates sub-second precision, so only guarantee monotonicity.
        assert user.updated_at >= before
        assert user.first_name == "Refreshed"


class TestUserManager:
    """Manager factory methods and their guards."""

    def test_create_user_defaults(self, db):
        person = User.objects.create_user(email="plain@flashwear.test")
        assert person.is_active is True
        assert person.is_staff is False
        assert person.is_superuser is False
        assert not person.has_usable_password()

    def test_create_user_sets_password_hash(self, db):
        person = User.objects.create_user(
            email="hashed@flashwear.test",
            password="Str0ng-Passw0rd!",
        )
        assert person.password != "Str0ng-Passw0rd!"
        assert person.check_password("Str0ng-Passw0rd!")

    def test_create_user_requires_email(self, db):
        with pytest.raises(ValueError, match="email"):
            User.objects.create_user(email="", password="Str0ng-Passw0rd!")

    def test_create_superuser(self, admin_user):
        assert admin_user.is_active is True
        assert admin_user.is_staff is True
        assert admin_user.is_superuser is True

    def test_create_superuser_requires_staff(self, db):
        with pytest.raises(ValueError, match="is_staff"):
            User.objects.create_superuser(
                email="nostaff@flashwear.test",
                password="Str0ng-Passw0rd!",
                is_staff=False,
            )

    def test_create_superuser_requires_superuser_flag(self, db):
        with pytest.raises(ValueError, match="is_superuser"):
            User.objects.create_superuser(
                email="nosuper@flashwear.test",
                password="Str0ng-Passw0rd!",
                is_superuser=False,
            )

    def test_get_by_natural_key_uses_email(self, user):
        assert User.objects.get_by_natural_key(user.email) == user


class TestEmailBackend:
    """The email authentication backend."""

    def test_only_custom_backend_is_configured(self):
        from django.conf import settings

        assert settings.AUTHENTICATION_BACKENDS == ["apps.accounts.backends.EmailBackend"]

    def test_backend_accepts_email_kwarg(self, user):
        assert authenticate(email=user.email, password="Str0ng-Passw0rd!") == user

    def test_backend_does_not_leak_password_timings_for_unknown_user(self, db):
        """Unknown addresses must still run the hasher, not short-circuit."""
        from apps.accounts.backends import EmailBackend

        backend = EmailBackend()
        assert backend.authenticate(None, username="ghost@flashwear.test", password="x") is None

    def test_inactive_user_password_is_not_compared(self, db):
        """``user_can_authenticate`` must short-circuit before ``check_password``."""
        inactive = User.objects.create_user(
            email="locked@flashwear.test",
            password="Str0ng-Passw0rd!",
            is_active=False,
        )
        inactive.set_unusable_password()
        inactive.save()
        assert authenticate(username="locked@flashwear.test", password="Str0ng-Passw0rd!") is None

    def test_multiple_objects_returned_falls_back_to_first_match(self, db, monkeypatch):
        """Defensive path: legacy rows may predate the lowercase-email normalisation.

        The unique constraint makes this unreachable through the ORM, so the manager is
        stubbed to raise ``MultipleObjectsReturned`` and the fallback branch is exercised
        directly.
        """
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        person = user_model.objects.create_user(
            email="dup@flashwear.test", password="Str0ng-Passw0rd!"
        )

        def always_ambiguous(*args, **kwargs):
            raise user_model.MultipleObjectsReturned("duplicate emails")

        monkeypatch.setattr(user_model._default_manager, "get", always_ambiguous)

        from apps.accounts.backends import EmailBackend

        assert (
            EmailBackend().authenticate(
                None, username="dup@flashwear.test", password="Str0ng-Passw0rd!"
            )
            == person
        )

    def test_multiple_objects_returned_with_no_match_returns_none(self, db, monkeypatch):
        from django.contrib.auth import get_user_model

        user_model = get_user_model()

        def always_ambiguous(*args, **kwargs):
            raise user_model.MultipleObjectsReturned("duplicate emails")

        monkeypatch.setattr(user_model._default_manager, "get", always_ambiguous)

        from apps.accounts.backends import EmailBackend

        assert (
            EmailBackend().authenticate(None, username="ghost@flashwear.test", password="x") is None
        )

    def test_authenticates_with_correct_password(self, user):
        assert authenticate(username=user.email, password="Str0ng-Passw0rd!") == user

    def test_authenticates_case_insensitively(self, user):
        assert authenticate(username=user.email.upper(), password="Str0ng-Passw0rd!") == user

    def test_rejects_wrong_password(self, user):
        assert authenticate(username=user.email, password="wrong-password") is None

    def test_rejects_unknown_email(self, db):
        assert authenticate(username="ghost@flashwear.test", password="Str0ng-Passw0rd!") is None

    def test_rejects_inactive_user(self, db):
        inactive = User.objects.create_user(
            email="inactive@flashwear.test",
            password="Str0ng-Passw0rd!",
            is_active=False,
        )
        assert authenticate(username=inactive.email, password="Str0ng-Passw0rd!") is None

    def test_rejects_missing_credentials(self, user):
        assert authenticate(username=user.email, password=None) is None
        assert authenticate(username=None, password="Str0ng-Passw0rd!") is None


class TestAccountUrls:
    """``LOGIN_URL`` must resolve, or every ``login_required`` redirect 404s."""

    def test_login_url_is_routable(self):
        from django.conf import settings

        assert settings.LOGIN_URL == "/accounts/login/"
        assert resolve(settings.LOGIN_URL).url_name == "login"

    def test_login_redirect_targets_are_routable(self):
        """A redirect target that 404s turns every successful sign-in into a dead end."""
        from django.conf import settings
        from django.shortcuts import resolve_url

        for target in (settings.LOGIN_REDIRECT_URL, settings.LOGOUT_REDIRECT_URL):
            assert resolve(resolve_url(target)).url_name == "home"


class TestLoginView:
    """The sign-in page and its POST handling."""

    def test_get_renders_form(self, client):
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 200
        assert response.templates[0].name == "accounts/login.html"
        assert "csrfmiddlewaretoken" in response.content.decode()

    def test_form_asks_for_email_and_password(self, client):
        response = client.get(reverse("accounts:login"))
        form = response.context["form"]
        assert list(form.fields) == ["username", "password"]

    def test_successful_login_redirects_home(self, client, user):
        response = client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": "Str0ng-Passw0rd!"},
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("core:home")
        assert client.session["_auth_user_id"] == str(user.pk)

    def test_login_is_case_insensitive(self, client, user):
        response = client.post(
            reverse("accounts:login"),
            {"username": user.email.upper(), "password": "Str0ng-Passw0rd!"},
        )
        assert response.status_code == 302

    def test_bad_credentials_rerender_without_session(self, client, user):
        response = client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": "wrong-password"},
        )
        assert response.status_code == 200
        assert "_auth_user_id" not in client.session
        assert "Please enter a correct email address and password" in response.content.decode()

    def test_inactive_user_cannot_sign_in(self, client, user):
        user.is_active = False
        user.save()
        response = client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": "Str0ng-Passw0rd!"},
        )
        assert response.status_code == 200
        assert "_auth_user_id" not in client.session

    def test_next_is_honoured_when_safe(self, client, user):
        response = client.post(
            f"{reverse('accounts:login')}?next=/admin/",
            {"username": user.email, "password": "Str0ng-Passw0rd!"},
        )
        assert response["Location"] == "/admin/"

    def test_next_cannot_redirect_off_site(self, client, user):
        """An open redirect via ``?next=`` would be a phishing primitive."""
        response = client.post(
            f"{reverse('accounts:login')}?next=https://evil.example/steal",
            {"username": user.email, "password": "Str0ng-Passw0rd!"},
        )
        assert response["Location"] == reverse("core:home")

    def test_authenticated_user_is_bounced(self, client, user):
        client.force_login(user)
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 302
        assert response["Location"] == reverse("core:home")


class TestLoginThrottle:
    """Brute-force protection for the storefront sign-in form."""

    @pytest.fixture(autouse=True)
    def _clear_counters(self):
        """Counters live in the cache, so they would otherwise leak between tests."""
        cache.clear()
        yield
        cache.clear()

    @staticmethod
    def _post(client, email, password="wrong-password", **extra):
        return client.post(
            reverse("accounts:login"), {"username": email, "password": password}, **extra
        )

    def test_repeated_failures_eventually_block(self, client, user, settings):
        settings.LOGIN_MAX_FAILURES_PER_ACCOUNT = 3
        for _ in range(3):
            assert self._post(client, user.email).status_code == 200
        blocked = self._post(client, user.email)
        assert blocked.status_code == 429
        assert blocked["Retry-After"] == str(settings.LOGIN_FAILURE_WINDOW_SECONDS)
        assert "Too many failed sign-in attempts" in blocked.content.decode()

    def test_correct_password_is_refused_once_blocked(self, client, user, settings):
        """The limit must apply before authentication, not after."""
        settings.LOGIN_MAX_FAILURES_PER_ACCOUNT = 1
        self._post(client, user.email)
        response = self._post(client, user.email, password="Str0ng-Passw0rd!")
        assert response.status_code == 429
        assert "_auth_user_id" not in client.session

    def test_successful_login_resets_the_counter(self, client, user, settings):
        settings.LOGIN_MAX_FAILURES_PER_ACCOUNT = 2
        self._post(client, user.email)
        assert self._post(client, user.email, password="Str0ng-Passw0rd!").status_code == 302
        client.post(reverse("accounts:logout"))
        # A fresh set of mistakes must not immediately re-lock the account: the window allows two
        # more failures, and only the third trips it again.
        assert self._post(client, user.email).status_code == 200
        assert self._post(client, user.email).status_code == 200
        assert self._post(client, user.email).status_code == 429

    def test_successful_login_is_never_counted_against_the_limit(self, client, user, settings):
        settings.LOGIN_MAX_FAILURES_PER_ACCOUNT = 2
        for _ in range(5):
            assert self._post(client, user.email, password="Str0ng-Passw0rd!").status_code == 302
            client.post(reverse("accounts:logout"))

    def test_lockout_is_per_account(self, client, user, settings):
        settings.LOGIN_MAX_FAILURES_PER_ACCOUNT = 2
        for _ in range(3):
            self._post(client, user.email)
        assert self._post(client, user.email).status_code == 429
        # A different account from the same address is unaffected.
        assert self._post(client, "victim@flashwear.test").status_code == 200

    def test_per_ip_limit_covers_multiple_accounts(self, client, settings):
        settings.LOGIN_MAX_FAILURES_PER_IP = 3
        for index in range(3):
            self._post(client, f"victim{index}@flashwear.test")
        # Each attempt used a different account, so only the IP counter can be responsible.
        assert self._post(client, "other@flashwear.test").status_code == 429

    def test_counters_expire(self, client, user, settings):
        """Counters must self-clean: the configured window is what bounds a lockout."""
        settings.LOGIN_MAX_FAILURES_PER_ACCOUNT = 1
        settings.LOGIN_FAILURE_WINDOW_SECONDS = 60
        self._post(client, user.email)
        throttle = LoginThrottle()
        key = throttle._account_key(user.email)
        assert throttle.window == 60
        assert cache.get(key) == 1
        # Stand in for the window elapsing rather than sleeping through it.
        cache.delete(key)
        assert self._post(client, user.email).status_code == 200

    def test_throttled_response_disables_the_submit_button(self, client, user, settings):
        settings.LOGIN_MAX_FAILURES_PER_ACCOUNT = 1
        self._post(client, user.email)
        content = self._post(client, user.email).content.decode()
        assert "disabled" in content

    def test_throttle_state_is_not_stored_in_plaintext(self, client, user, settings):
        settings.LOGIN_MAX_FAILURES_PER_ACCOUNT = 1
        self._post(client, user.email)
        throttle = LoginThrottle()
        account_key = throttle._account_key(user.email)
        assert user.email not in account_key
        assert cache.get(account_key) == 1

    def test_forwarded_for_is_ignored_unless_trusted(self, user, settings):
        """An attacker-controlled header must not mint a fresh counter per request."""
        settings.USE_X_FORWARDED_FOR = False
        request = RequestFactory().post("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="1.2.3.4")
        assert _client_ip(request) == "10.0.0.1"

        settings.USE_X_FORWARDED_FOR = True
        assert _client_ip(request) == "1.2.3.4"

    def test_x_forwarded_for_chain_uses_the_client_hop(self, settings):
        settings.USE_X_FORWARDED_FOR = True
        request = RequestFactory().post(
            "/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.9"
        )
        assert _client_ip(request) == "1.2.3.4"

    def test_defaults_are_sane(self, settings):
        throttle = LoginThrottle()
        assert 0 < throttle.max_per_account <= throttle.max_per_ip
        assert throttle.window > 0
        assert settings.LOGIN_MAX_FAILURES_PER_ACCOUNT == throttle.max_per_account
        assert settings.LOGIN_MAX_FAILURES_PER_IP == throttle.max_per_ip
        assert settings.LOGIN_FAILURE_WINDOW_SECONDS == throttle.window

    def test_throttled_user_can_still_reach_other_pages(self, client, user, settings):
        settings.LOGIN_MAX_FAILURES_PER_ACCOUNT = 1
        self._post(client, user.email)
        assert self._post(client, user.email).status_code == 429
        assert client.get(reverse("core:home")).status_code == 200


class TestLogoutView:
    """Sign-out must be POST-only so a stray request cannot end a session."""

    def test_get_is_not_allowed(self, client, user):
        client.force_login(user)
        assert client.get(reverse("accounts:logout")).status_code == 405
        assert "_auth_user_id" in client.session

    def test_post_ends_session_and_redirects(self, client, user):
        client.force_login(user)
        response = client.post(reverse("accounts:logout"))
        assert response.status_code == 302
        assert response["Location"] == reverse("core:home")
        assert "_auth_user_id" not in client.session

    def test_post_without_csrf_is_rejected(self, client, user):
        from django.test import Client

        csrf_disabled = Client(enforce_csrf_checks=True)
        csrf_disabled.force_login(user)
        response = csrf_disabled.post(reverse("accounts:logout"))
        assert response.status_code == 403
        assert "_auth_user_id" in csrf_disabled.session

    def test_anonymous_post_is_harmless(self, client):
        response = client.post(reverse("accounts:logout"))
        assert response.status_code == 302
        assert "_auth_user_id" not in client.session


class TestNavbarAuthState:
    """The navbar must reflect real session state instead of dead placeholders."""

    @staticmethod
    def _navbar(content: str) -> str:
        """Isolate the navbar so links elsewhere on the page cannot mask a result."""
        start = content.index("<header")
        end = content.index("</header>")
        return content[start:end]

    def test_anonymous_navbar_links_to_login(self, client):
        navbar = self._navbar(client.get(reverse("core:home")).content.decode())
        assert reverse("accounts:login") in navbar
        assert reverse("accounts:logout") not in navbar

    def test_authenticated_navbar_offers_logout_form(self, client, user):
        client.force_login(user)
        navbar = self._navbar(client.get(reverse("core:home")).content.decode())
        assert f'action="{reverse("accounts:logout")}"' in navbar
        assert reverse("accounts:login") not in navbar

    def test_staff_navbar_links_to_admin(self, client, admin_user):
        client.force_login(admin_user)
        navbar = self._navbar(client.get(reverse("core:home")).content.decode())
        assert reverse("admin:index") in navbar

    def test_customer_navbar_hides_admin_link(self, client, user):
        client.force_login(user)
        navbar = self._navbar(client.get(reverse("core:home")).content.decode())
        assert reverse("admin:index") not in navbar

    def test_logout_uses_post_not_get(self, client, user):
        """A GET logout link would make logout CSRF-exploitable."""
        client.force_login(user)
        navbar = self._navbar(client.get(reverse("core:home")).content.decode())
        assert f'href="{reverse("accounts:logout")}"' not in navbar
        assert f'action="{reverse("accounts:logout")}"' in navbar
