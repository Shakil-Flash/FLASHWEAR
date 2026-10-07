"""Account views.

Phase 1 supplied the sign-in/sign-out pair: Django's built-ins plus the cache-backed failed-login
limit in :mod:`apps.accounts.throttling`. Those two classes keep their Phase 1 behaviour exactly;
Phase 2 adds the rest of the customer account surface around them -- registration, email
verification, password reset/change, the dashboard, the profile editor, the address book and
account closure.

Every authenticated view resolves its object through the customer's own queryset, so swapping an
id in a URL cannot reach another customer's row.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.password_validation import get_default_password_validators
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.http import urlsafe_base64_decode
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST
from django.views.generic import (
    CreateView,
    DeleteView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
)
from django.views.generic.edit import View

from apps.accounts import services
from apps.accounts.forms import (
    AccountDeletionForm,
    AddressForm,
    EmailVerificationResendForm,
    ProfileForm,
    RegistrationForm,
)
from apps.accounts.models import Address
from apps.accounts.throttling import AttemptThrottle, LoginThrottle

User = get_user_model()

# ``AuthenticationForm.clean()`` hard-codes the name of its credential input as "username".
# ``User.USERNAME_FIELD`` decides only what the backend treats that value as -- for us, an email
# address -- so the throttle has to read and count the posted "username" field, not "email".
USERNAME_FIELD = "username"

# Sessions older than this bounce the customer back to sign-in with a "next" back to where they
# were. Matches ``SESSION_COOKIE_AGE`` in production (two weeks).
SESSION_EXPIRED_FLAG = "flashwear_session_expired"


class LoginView(auth_views.LoginView):
    """Email + password sign-in.

    Uses ``EmailBackend`` via ``AUTHENTICATION_BACKENDS``, so the username field is the email
    address. ``redirect_authenticated_user`` stops a signed-in visitor from looping between the
    login page and the homepage.
    """

    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    throttle: LoginThrottle

    def get_throttle(self) -> LoginThrottle:
        if not hasattr(self, "throttle"):
            self.throttle = LoginThrottle()
        return self.throttle

    def form_valid(self, form):
        # A success clears the counters so a customer who fumbled a few passwords is not left
        # carrying a stale limit.
        self.get_throttle().reset(self.request, form.get_user().email)
        response = super().form_valid(form)
        # "Remember me" only changes how long the session cookie lives -- no second credential is
        # issued. Unchecked falls back to the configured session age.
        if form.data.get("remember_me"):
            self.request.session.set_expiry(settings.ACCOUNT_REMEMBER_ME_DAYS * 24 * 60 * 60)
        else:
            self.request.session.set_expiry(settings.SESSION_COOKIE_AGE)
        messages.success(
            self.request,
            _("%(name)s, welcome back.") % {"name": self.request.user.get_short_name()},
        )
        return response

    def form_invalid(self, form):
        self.get_throttle().record_failure(self.request, form.data.get(USERNAME_FIELD, ""))
        return super().form_invalid(form)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if self.request.session.pop(SESSION_EXPIRED_FLAG, False):
            # A message rather than a form error: the customer did nothing wrong, and an error
            # on an unbound form would land in the field-error region of the page.
            messages.info(self.request, _("Your session expired. Please sign in again."))
        return form

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            username = request.POST.get(USERNAME_FIELD, "")
            decision = self.get_throttle().check(request, username)
            if decision.blocked:
                response = render(
                    request,
                    self.template_name,
                    {
                        "form": self.get_form(),
                        "throttled": True,
                        "retry_after": decision.retry_after,
                    },
                    status=429,
                )
                # Tell the client when it is worth trying again.
                response["Retry-After"] = str(decision.retry_after)
                return response
        return super().dispatch(request, *args, **kwargs)


class AttemptThrottleMixin:
    """429 gate in front of POST for endpoints whose abuse is volume itself.

    Password reset, verification resend and registration all send mail or write
    rows even when each individual request looks legitimate, so the throttle
    counts *every* attempt from a client address rather than waiting for
    failures. The blocked response re-renders the view's own form with a
    non-field error and a ``Retry-After`` header, mirroring the sign-in
    throttle; attempts are recorded only when the request is allowed through,
    so blocked traffic cannot extend its own window.
    """

    throttle_action: str

    def get_throttle(self) -> AttemptThrottle:
        if not hasattr(self, "_attempt_throttle"):
            self._attempt_throttle = AttemptThrottle(self.throttle_action)
        return self._attempt_throttle

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            decision = self.get_throttle().check(request)
            if decision.blocked:
                form = self.get_form()
                form.add_error(
                    None,
                    _("Too many attempts. Please wait %(seconds)s seconds and try again.")
                    % {"seconds": decision.retry_after},
                )
                response = self.form_invalid(form)
                response.status_code = 429
                # Tell the client when it is worth trying again.
                response["Retry-After"] = str(decision.retry_after)
                return response
            self.get_throttle().record(request)
        return super().dispatch(request, *args, **kwargs)


class LogoutView(auth_views.LogoutView):
    """Sign out.

    Django 5 only accepts POST here, which is what makes logout CSRF-safe: a stray
    ``<img src="/accounts/logout/">`` cannot end someone's session.
    """

    http_method_names = ["post", "options"]


class RegisterView(AttemptThrottleMixin, FormView):
    """Create an account and start the verification flow.

    A successful registration signs the customer in -- the account is usable straight away -- but
    the address stays unverified until the emailed link is followed, and the dashboard keeps
    reminding the customer until it is.
    """

    template_name = "account/register.html"
    form_class = RegistrationForm
    throttle_action = "register"

    @method_decorator(sensitive_post_parameters("password1", "password2"))
    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("account:dashboard")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["password_help"] = password_policy_help()
        return context

    def form_valid(self, form):
        try:
            user = form.save()
        except services.EmailAlreadyRegistered:
            # The form already checks for this; the service check exists because two people can
            # submit the same address at the same instant and the row write is the real gate.
            form.add_error("email", _("That email address was just taken. Try signing in instead."))
            return self.form_invalid(form)
        services.send_verification_email(user, self.request)
        login(self.request, user)
        messages.success(
            self.request,
            _("Account created. Check your inbox for the link that verifies your email."),
        )
        return redirect("account:dashboard")


def password_policy_help() -> list[str]:
    """The configured password validators, as customer-facing sentences."""
    hints = []
    for validator in get_default_password_validators():
        help_text = getattr(validator, "get_help_text", None)
        if callable(help_text):
            hints.append(str(help_text()))
    return hints


class VerifyEmailView(View):
    """Consume a signed verification link.

    Safe for anonymous visitors: the token itself is the credential, and Django's generator binds
    it to one account and one password state. An expired, edited, unknown or already-used link all
    report the same thing, because distinguishing them tells an attacker which guesses landed.
    """

    template_name = "account/verify_email.html"
    http_method_names = ["get"]

    @method_decorator(never_cache)
    def dispatch(self, request, uidb64, token, *args, **kwargs):
        # One flag, not a status: an invalid link is "try again", and the page tells the signed-in
        # customer how. Guessing must not be possible from the difference between "no such user"
        # and "bad token".
        self.verified = False
        user = _user_from_uid(uidb64)
        if user is not None:
            self.verified = services.verify_email(user, token)
        response = super().dispatch(request, *args, uidb64=uidb64, token=token, **kwargs)
        if self.verified:
            messages.success(self.request, _("Your email address is verified."))
        else:
            messages.error(
                self.request,
                _("That verification link is invalid or has expired."),
            )
        return response

    def get(self, request, uidb64, token):
        return render(
            request,
            self.template_name,
            {
                "verified": self.verified,
                "can_resend": request.user.is_authenticated and not request.user.is_email_verified,
            },
        )


def _user_from_uid(uidb64: str):
    """Return the active user named by a ``urlsafe_base64`` pk, or ``None``.

    Every failure mode -- bad base64, non-numeric pk, deleted account, deactivated account --
    collapses to ``None`` so the caller has one code path and the page leaks nothing.
    """
    if not uidb64:
        return None
    try:
        pk = urlsafe_base64_decode(uidb64).decode()
        return User.objects.get(pk=pk, is_active=True)
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError, User.DoesNotExist):
        return None


class ResendVerificationEmailView(AttemptThrottleMixin, FormView):
    """Send another verification link.

    POST-only with an explicit checkbox, so a stray reload cannot mail the customer in a loop and
    the customer's mail provider is never used as an amplifier.
    """

    template_name = "account/verify_email_resend.html"
    form_class = EmailVerificationResendForm
    success_url = reverse_lazy("account:dashboard")
    throttle_action = "resend_verification"

    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        # The URL is registered through ``account_urls.protected``, so ``login_required`` has
        # already bounced anonymous visitors by the time this runs. The check is kept anyway: this
        # is the one endpoint that sends mail on request, and an accidental route re-registration
        # must not turn it into an open relay for whoever knows the URL.
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        if self.request.user.is_email_verified:
            messages.info(self.request, _("Your address is already verified."))
        else:
            services.send_verification_email(self.request.user, self.request)
            messages.success(self.request, _("A new verification link is on its way."))
        return super().form_valid(form)


class PasswordResetView(AttemptThrottleMixin, auth_views.PasswordResetView):
    """Ask for a reset link.

    Django's ``PasswordResetForm`` sends nothing for an unknown address and its confirmation page
    says nothing either; this view only adds the audit row, so the non-revealing behaviour is
    inherited rather than re-implemented.
    """

    throttle_action = "password_reset"
    template_name = "account/password_reset_form.html"
    email_template_name = "registration/password_reset_email.txt"
    html_email_template_name = "registration/password_reset_email.html"
    subject_template_name = "registration/password_reset_email_subject.txt"
    success_url = reverse_lazy("account:password-reset-done")

    def form_valid(self, form):
        services.record_password_reset_request(self.request)
        return super().form_valid(form)

    def form_invalid(self, form):
        # An invalid form (malformed address) is still answered with the neutral wording, so the
        # page cannot be used to probe which addresses exist.
        form.add_error(
            None,
            _("If an account exists for this email, you'll receive reset instructions."),
        )
        return super().form_invalid(form)


class PasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "account/password_reset_done.html"


def _notify_password_changed(user, event) -> None:
    """Phase 17: security notice after a password change.

    Keyed to the audit row so a replayed form_valid cannot double-notify; if the audit
    write itself failed, fall back to a minute-granularity key -- the customer must still
    hear about a change to their password.
    """
    import time

    from apps.notifications.models import NotificationType
    from apps.notifications.services.events import emit

    key = (
        f"account_event:{event.pk}"
        if event is not None
        else f"user:{user.pk}:password_changed:{int(time.time()) // 60}"
    )
    emit(
        notification_type=NotificationType.PASSWORD_CHANGED,
        user=user,
        idempotency_key=key,
        context={},
        action_url="/account/security/",
        related_object_type="account_event",
        related_object_id=getattr(event, "pk", None),
    )


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """Set a new password from a reset link."""

    template_name = "account/password_reset_confirm.html"
    success_url = reverse_lazy("account:password-reset-complete")
    post_reset_login = False

    def form_valid(self, form):
        response = super().form_valid(form)
        # Django rotates the session on success, then we sign the customer in on the fresh one.
        # A pre-reset session is never reused: the reset may have been triggered by someone else.
        event = services.record_account_event(
            form.user, "password_changed", metadata={"via": "reset"}
        )
        _notify_password_changed(form.user, event)
        login(self.request, form.user)
        messages.success(self.request, _("Your password has been changed."))
        return response


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "account/password_reset_complete.html"


class PasswordChangeView(auth_views.PasswordChangeView):
    """Change the password while signed in.

    Requires the current password, runs the new one past the project's validators, and re-keys
    the session hash so this customer stays signed in on every device without invalidating any
    other session.
    """

    template_name = "account/password_change.html"
    success_url = reverse_lazy("account:security")

    def form_valid(self, form):
        response = super().form_valid(form)
        event = services.record_account_event(
            self.request.user,
            "password_changed",
            request=self.request,
            metadata={"via": "account"},
        )
        _notify_password_changed(self.request.user, event)
        messages.success(self.request, _("Your password has been changed."))
        return response


class AccountDashboardView(TemplateView):
    """``/account/`` -- the customer control centre and personal style home.

    Phase 31 elevates this from an administrative dashboard to a fashion profile:
    style signature, style evolution, wardrobe connection, outfit continuity,
    wishlist intelligence, and human-reason recommendations, while preserving
    all navigation and account status landmarks.
    """

    template_name = "account/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        addresses = user.addresses.all()

        from datetime import timedelta

        from django.utils import timezone

        from apps.catalog.merchandising import get_recently_viewed
        from apps.catalog.selectors import homepage_new_arrivals
        from apps.closet.models.items import ClosetItem
        from apps.closet.models.outfits import Outfit
        from apps.engagement.services.loyalty import balance_for
        from apps.recommendations.services import get_customer_recommendations
        from apps.shop.services import get_cart_for_request
        from apps.styling.services.style_profile import (
            get_style_evolution_insight,
            get_style_profile_summary,
        )

        recently_viewed = get_recently_viewed(self.request, limit=4)
        style_profile = get_style_profile_summary(user)
        style_evolution = get_style_evolution_insight(user)
        recommendations = get_customer_recommendations(user, limit=4)

        # Wardrobe & Outfits
        closet_items = (
            ClosetItem.objects.filter(user=user, status=ClosetItem.Status.ACTIVE)
            .select_related("variant__product")
            .order_by("-created_at")[:4]
        )
        closet_count = ClosetItem.objects.filter(
            user=user, status=ClosetItem.Status.ACTIVE
        ).count()

        saved_outfits = (
            Outfit.objects.filter(user=user, status=Outfit.Status.SAVED)
            .prefetch_related("items__item")
            .order_by("-updated_at")[:3]
        )
        saved_outfits_count = Outfit.objects.filter(
            user=user, status=Outfit.Status.SAVED
        ).count()
        draft_outfit = (
            Outfit.objects.filter(user=user, status=Outfit.Status.DRAFT)
            .order_by("-updated_at")
            .first()
        )

        # Wishlist intelligence preview
        wishlist_preview = []
        wishlist_count = 0
        wishlist = getattr(user, "wishlist", None)
        if wishlist is not None:
            now = timezone.now()
            items_qs = (
                wishlist.items.select_related("product", "variant")
                .order_by("-created_at")[:4]
            )
            wishlist_count = wishlist.items.count()
            for w_item in items_qs:
                stock_units = 0
                if w_item.variant:
                    stock_row = getattr(w_item.variant, "stock", None)
                    stock_units = stock_row.available if stock_row else 0
                elif w_item.product:
                    for v in w_item.product.purchasable_variants:
                        s = getattr(v, "stock", None)
                        if s:
                            stock_units += max(0, s.available)

                is_on_sale = False
                if w_item.variant:
                    is_on_sale = getattr(w_item.variant, "is_discounted", False)
                elif w_item.product:
                    is_on_sale = getattr(w_item.product, "is_on_sale", False)

                wishlist_preview.append(
                    {
                        "item": w_item,
                        "product": w_item.product,
                        "variant": w_item.variant,
                        "is_low_stock": 0 < stock_units <= 5,
                        "is_sold_out": stock_units == 0,
                        "is_on_sale": is_on_sale,
                        "is_recently_added": (now - w_item.created_at) < timedelta(days=14),
                        "stock": stock_units,
                    }
                )

        # Recent Orders
        recent_orders = (
            user.orders.select_related("payment")
            .prefetch_related("items__variant")
            .order_by("-created_at")[:2]
        )
        orders_count = user.orders.count()

        # Continuity & Loyalty
        cart = get_cart_for_request(self.request)
        cart_item_count = cart.get_total_quantity() if cart else 0
        loyalty_points = balance_for(user)
        new_drops = homepage_new_arrivals(limit=4)

        is_cold_start = (
            not style_profile.get("is_complete")
            and closet_count == 0
            and orders_count == 0
            and wishlist_count == 0
            and not bool(recently_viewed)
        )

        context.update(
            {
                "profile": user.profile,
                "addresses": addresses,
                "address_count": addresses.count(),
                "default_shipping": next((a for a in addresses if a.is_default_shipping), None),
                "recently_viewed": recently_viewed,
                "style_profile": style_profile,
                "style_evolution": style_evolution,
                "recommendations": recommendations,
                "closet_items": closet_items,
                "closet_count": closet_count,
                "saved_outfits": saved_outfits,
                "saved_outfits_count": saved_outfits_count,
                "draft_outfit": draft_outfit,
                "wishlist_preview": wishlist_preview,
                "wishlist_count": wishlist_count,
                "recent_orders": recent_orders,
                "orders_count": orders_count,
                "cart_item_count": cart_item_count,
                "loyalty_points": loyalty_points,
                "new_drops": new_drops,
                "is_cold_start": is_cold_start,
            }
        )
        return context


class ProfileUpdateView(FormView):
    """Edit names, avatar, contact details and preferences."""

    template_name = "account/profile.html"
    form_class = ProfileForm
    success_url = reverse_lazy("account:profile")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        user = self.request.user
        profile = user.profile
        initial.update(
            {
                "first_name": user.first_name,
                "last_name": user.last_name,
                "phone": profile.phone,
                "date_of_birth": profile.date_of_birth,
                "preferred_language": profile.preferred_language,
                "preferred_currency": profile.preferred_currency,
                "marketing_email_opt_in": profile.marketing_email_opt_in,
                "marketing_sms_opt_in": profile.marketing_sms_opt_in,
            }
        )
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["profile"] = self.request.user.profile
        context["avatar_limit_mb"] = settings.ACCOUNT_AVATAR_MAX_BYTES // (1024 * 1024)
        return context

    def form_valid(self, form):
        # The form writes both rows; the view only reports the outcome.
        form.save()
        messages.success(self.request, _("Your profile has been updated."))
        return super().form_valid(form)


class SecurityView(TemplateView):
    """Password change, verification state and the entry point to account closure."""

    template_name = "account/security.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update(
            {
                "has_usable_password": user.has_usable_password(),
                "member_since": user.date_joined,
            }
        )
        return context


class AddressListView(ListView):
    """The address book."""

    template_name = "account/address_list.html"
    context_object_name = "addresses"
    paginate_by = 20

    def get_queryset(self):
        # Ownership-scoped: this view has no way to ask for somebody else's addresses.
        return self.request.user.addresses.all()


class AddressCreateView(CreateView):
    """Add an address."""

    model = Address
    form_class = AddressForm
    template_name = "account/address_form.html"
    success_url = reverse_lazy("account:addresses")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.user = self.request.user
        response = super().form_valid(form)
        services.record_account_event(
            self.request.user,
            "address_created",
            request=self.request,
            metadata={"address_id": self.object.pk, "default": self.object.is_default_shipping},
        )
        messages.success(self.request, _("Address saved."))
        return response


class AddressUpdateView(UpdateView):
    """Edit an address the signed-in customer owns."""

    model = Address
    form_class = AddressForm
    template_name = "account/address_form.html"
    success_url = reverse_lazy("account:addresses")

    def get_queryset(self):
        return self.request.user.addresses.all()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        services.record_account_event(
            self.request.user,
            "address_updated",
            request=self.request,
            metadata={"address_id": self.object.pk},
        )
        messages.success(self.request, _("Address updated."))
        return response


class AddressDeleteView(DeleteView):
    """Remove an address the signed-in customer owns."""

    model = Address
    template_name = "account/address_confirm_delete.html"
    context_object_name = "address"
    success_url = reverse_lazy("account:addresses")

    def get_queryset(self):
        return self.request.user.addresses.all()

    def form_valid(self, form):
        was_default = self.get_object().is_default_shipping
        response = super().form_valid(form)
        services.record_account_event(
            self.request.user,
            "address_deleted",
            request=self.request,
            metadata={"was_default": was_default},
        )
        messages.success(self.request, _("Address removed."))
        return response


@method_decorator(require_POST, name="dispatch")
class AddressSetDefaultView(View):
    """Promote one of the customer's own addresses to a default."""

    KINDS = {"shipping": "is_default_shipping", "billing": "is_default_billing"}

    def post(self, request, pk: int, kind: str):
        if kind not in self.KINDS:
            raise PermissionDenied("Unknown default address kind.")
        address = get_object_or_404(Address, pk=pk, user=request.user)
        try:
            services.set_default_address(request.user, address, kind=kind, request=request)
        except ValueError as error:
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                _("%(kind)s address set as your default.")
                % {"kind": _("Delivery") if kind == "shipping" else _("Billing")},
            )
        return redirect("account:addresses")


class AccountDeleteView(FormView):
    """Deactivate the account after a password-confirmed confirmation.

    See :func:`apps.accounts.services.deactivate_account` for what is preserved and why. Nothing
    is deleted here, which is why the copy on the page says "deactivate" rather than "delete".
    """

    template_name = "account/delete.html"
    form_class = AccountDeletionForm
    success_url = reverse_lazy("account:deleted")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["retention_days"] = settings.ACCOUNT_DATA_RETENTION_DAYS
        return context

    def form_valid(self, form):
        services.deactivate_account(self.request.user, request=self.request)
        logout(self.request)
        messages.success(
            self.request,
            _("Your account is deactivated. Sorry to see you go."),
        )
        return super().form_valid(form)


class AccountDeletedView(TemplateView):
    """Post-closure confirmation. Reachable while signed out, hence no ``login_required``."""

    template_name = "account/deleted.html"


def redirect_to_login(next_path: str):
    """Send an anonymous visitor to sign-in, remembering where they were headed."""
    from django.contrib.auth.views import redirect_to_login as auth_redirect_to_login

    return auth_redirect_to_login(next_path, settings.LOGIN_URL)
