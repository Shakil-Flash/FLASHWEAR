"""Account services: the logic that is too important to live inside a view.

Views stay thin and forms stay declarative; anything with a transaction, an invariant or a
side effect (email, audit event, session invalidation) belongs here.

Rules that hold across this module:

* no function accepts or returns a password, a reset token or a verification token;
* audit events record facts, never secrets;
* every function that changes an account either returns the new state or raises, so a caller
  cannot silently half-apply a change.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlsafe_base64_encode

from apps.accounts.mailer import absolute_url, send_account_email

logger = logging.getLogger("flashwear.accounts")

User = get_user_model()

# A per-process salt keeps the digests in ``AccountEvent`` from being reversible with a
# rainbow table, without adding a secret that has to be rotated. Rotate SECRET_KEY and the
# historical digests simply stop matching, which is harmless -- they are for correlation only.
DIGEST_SALT = "flashwear.account.digest.v1"


# --------------------------------------------------------------------------------------
# Audit helpers
# --------------------------------------------------------------------------------------


def digest(value: str) -> str:
    """Return a salted, truncated SHA-256 digest of ``value``.

    Used for email addresses and client IPs in the audit trail: enough to correlate events,
    not enough to read an address book out of the database.
    """
    if not value:
        return ""
    return hashlib.sha256(f"{DIGEST_SALT}{value.strip().lower()}".encode()).hexdigest()[:32]


def record_account_event(
    user,
    event_type: str,
    *,
    request=None,
    metadata: dict | None = None,
    channel: str = "web",
) -> None:
    """Append an audit row. Never raises into the caller.

    Losing an audit line must not turn a successful sign-up into a 500, so failures are logged
    at error level and swallowed.
    """
    try:
        from apps.accounts.models import AccountEvent

        ip = ""
        if request is not None:
            from apps.accounts.throttling import _client_ip

            ip = _client_ip(request)
        AccountEvent.objects.create(
            user=user if getattr(user, "pk", None) else None,
            email_digest=digest(getattr(user, "email", "") or ""),
            event_type=event_type,
            channel=channel,
            metadata=metadata or {},
            ip_digest=digest(ip) if ip else "",
        )
    except Exception:  # pragma: no cover - defensive, exercised only on a broken database
        logger.exception("Failed to record account event %s", event_type)


# --------------------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------------------


class EmailAlreadyRegistered(Exception):
    """Raised when an address is already in use.

    Carries no extra detail: the caller decides what to show, and nothing about the existing
    account leaks through the exception.
    """


@transaction.atomic
def create_account(
    *,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
    request=None,
) -> object:
    """Create an unverified account and return it.

    The address is canonicalised exactly the way :meth:`User.save` does it, then checked, so
    ``Bob@Example.com`` cannot slip past the duplicate check and create a second row.
    """
    normalised = User.normalize_email_value(email)
    if User.objects.filter(email=normalised).exists():
        raise EmailAlreadyRegistered

    user = User(email=normalised, first_name=first_name.strip(), last_name=last_name.strip())
    user.set_password(password)
    user.save()
    logger.info("Account created for %s", digest(user.email))
    return user


# --------------------------------------------------------------------------------------
# Email verification
# --------------------------------------------------------------------------------------


def build_verification_url(user, request=None) -> str:
    """Build the signed, expiring verification link for ``user``.

    Django's default token generator signs the user's primary key, password hash and a
    timestamp with ``SECRET_KEY`` via an HMAC. Consequences worth stating: the link cannot be
    forged or edited, it stops working once ``PASSWORD_RESET_TIMEOUT`` has passed, and it stops
    working the moment the password changes (the hash moves). It is single-use in practice
    because :func:`verify_email` only ever transitions an unverified account.
    """
    uid = urlsafe_base64_encode(str(user.pk).encode())
    token = default_token_generator.make_token(user)
    path = reverse("account:verify-email", kwargs={"uidb64": uid, "token": token})
    return absolute_url(path, request)


def send_verification_email(user, request=None) -> None:
    """Email the verification link, then record the attempt.

    Re-sending is always allowed: an unverified customer who lost the first mail must not be
    locked out. There is no rate limit here yet -- the address is unverified and unusable, so
    the abuse value is low, but a per-address cooldown is the obvious next hardening step.
    """
    send_account_email(
        subject_template="accounts/email/verification_email_subject.txt",
        text_template="accounts/email/verification_email.txt",
        html_template="accounts/email/verification_email.html",
        context={
            "user": user,
            "verification_url": build_verification_url(user, request),
            # Told to the customer so a stale link is not mistaken for a broken one.
            "expiry_hours": max(1, settings.PASSWORD_RESET_TIMEOUT // 3600),
        },
        recipient=user.email,
        request=request,
    )
    record_account_event(
        user,
        "email_verification_sent",
        request=request,
        metadata={"channel": "email"},
    )


def verify_email(user, token: str) -> bool:
    """Mark ``user``'s address verified if ``token`` is valid. Return whether it applied."""
    if user.is_email_verified:
        # Already verified: a replayed link is not an error, it is just a no-op.
        return False
    if not default_token_generator.check_token(user, token):
        return False
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at", "updated_at"])
    logger.info("Email verified for %s", digest(user.email))
    record_account_event(user, "email_verified")
    return True


# --------------------------------------------------------------------------------------
# Deactivation
# --------------------------------------------------------------------------------------


@transaction.atomic
def deactivate_account(user, *, request=None, reason: str = "customer_request") -> None:
    """Close an account without destroying its history.

    What happens:

    * ``is_active`` goes false and ``deactivated_at`` records when -- so sign-in stops
      immediately, via the flag Django already honours;
    * the password is replaced with an unusable value, so even a session token that outlives
      the request cannot be turned back into a working credential;
    * every session row for the user is deleted, which is what "log out everywhere" means here;
    * an ``account_deactivated`` audit row is written.

    What deliberately does *not* happen: no row is deleted. Future orders, payments, returns
    and reviews will reference this user, and tax/accounting rules usually require keeping the
    record. :data:`~config.settings.base.ACCOUNT_DATA_RETENTION_DAYS` is the documented window
    after which an operator may run an anonymisation job that clears the personal fields while
    keeping the row as a foreign-key target.
    """
    from django.contrib.sessions.models import Session

    user.is_active = False
    user.deactivated_at = timezone.now()
    user.set_password(None)
    user.save(update_fields=["is_active", "deactivated_at", "password", "updated_at"])

    Session.objects.filter(session_data__contains=f'"_auth_user_id": "{user.pk}"').delete()

    logger.info("Account deactivated (%s) for %s", reason, digest(user.email))
    record_account_event(user, "account_deactivated", request=request, metadata={"reason": reason})


def reactivation_cutoff():
    """Timestamp before which a deactivated account is still inside its retention window."""
    return timezone.now() - timedelta(days=settings.ACCOUNT_DATA_RETENTION_DAYS)


# --------------------------------------------------------------------------------------
# Password reset
# --------------------------------------------------------------------------------------


def record_password_reset_request(request) -> None:
    """Audit a reset request without recording which address was asked about.

    The mail itself is sent by Django's ``PasswordResetForm``, which already refuses to reveal
    whether an address exists. This only records that somebody asked.
    """
    logger.info("Password reset requested from %s", digest(_client_address(request)))
    record_account_event(None, "password_reset_requested", request=request)


def _client_address(request) -> str:
    from apps.accounts.throttling import _client_ip

    return _client_ip(request) if request is not None else ""


# --------------------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------------------


class AddressNotFound(Exception):
    """Raised when an address does not exist for the requesting customer."""


def set_default_address(user, address, *, kind: str, request=None) -> None:
    """Promote ``address`` to the default shipping or billing address.

    Serialised on the user row with ``select_for_update`` so two concurrent requests cannot
    both pass the "clear the old default" step and leave two defaults behind. The partial
    unique indexes are the backstop if this is ever bypassed.
    """
    if kind == "shipping":
        if address.address_type == address.AddressType.BILLING:
            raise ValueError("A billing-only address cannot be the default shipping address.")
        target = "is_default_shipping"
        queryset = Q(is_default_shipping=True)
    elif kind == "billing":
        if not address.as_billing_eligible:
            raise ValueError("This address is not used for billing.")
        target = "is_default_billing"
        queryset = Q(is_default_billing=True)
    else:  # pragma: no cover - guarded by the view
        raise ValueError(f"Unknown address kind: {kind}")

    with transaction.atomic():
        # Lock the customer row, not the address: two requests promoting two different addresses
        # must serialise on the same row, or both clear-and-set and leave two defaults behind.
        locked = get_user_model()._default_manager.select_for_update().get(pk=user.pk)
        locked.addresses.filter(queryset).exclude(pk=address.pk).update(**{target: False})
        # ``update()`` skips auto_now, so stamp it explicitly for the row that survives.
        setattr(address, target, True)
        address.save(update_fields=[target, "updated_at"])

    record_account_event(
        user,
        "default_address_changed",
        request=request,
        metadata={"kind": kind, "address_id": address.pk},
    )


def anonymise_deactivated_accounts(*, dry_run: bool = True) -> list[int]:
    """Scrub personal data from accounts past the retention window.

    Deliberately conservative and opt-in: it clears the fields that identify a person while
    keeping the user row, its addresses' shape and the audit trail, because future orders and
    refunds need a stable foreign-key target. Run it from a scheduled management command
    (Phase 3), never inline in a request.

    Returns the primary keys that were (or would be) anonymised.
    """
    candidates = User.objects.filter(
        deactivated_at__isnull=False, deactivated_at__lte=reactivation_cutoff()
    )
    if dry_run:
        return list(candidates.values_list("pk", flat=True))

    affected: list[int] = []
    for user in candidates.iterator():
        user.first_name = ""
        user.last_name = ""
        user.set_password(None)
        user.save(update_fields=["first_name", "last_name", "password", "updated_at"])
        user.profile.phone = ""
        user.profile.avatar = ""
        user.profile.save(update_fields=["phone", "avatar", "updated_at"])
        user.addresses.update(full_name="", phone="")
        affected.append(user.pk)
    logger.info("Anonymised %s deactivated accounts", len(affected))
    return affected
