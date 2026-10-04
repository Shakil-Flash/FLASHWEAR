"""Serializers for the account API.

Written by hand rather than generated so that two rules are visible in the code rather than
implied by a ``fields = "__all__"``:

* ``password`` is write-only and never leaves the server;
* an address may only ever be read or written through the customer's own queryset, which the view
  enforces with ``get_queryset`` -- the serializer does not carry the user id at all.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.accounts.models import Address, Profile

User = get_user_model()


class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = (
            "avatar",
            "phone",
            "date_of_birth",
            "preferred_language",
            "preferred_currency",
            "marketing_email_opt_in",
            "marketing_sms_opt_in",
            "updated_at",
        )
        read_only_fields = ("updated_at",)


class UserSerializer(serializers.ModelSerializer):
    """The signed-in customer, as the API exposes them."""

    profile = ProfileSerializer()
    is_email_verified = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "is_email_verified",
            "date_joined",
            "profile",
        )
        read_only_fields = ("id", "email", "is_email_verified", "date_joined")


class AddressSerializer(serializers.ModelSerializer):
    """A saved address.

    ``user`` is absent from ``fields`` on purpose: the owner comes from the request, never from
    the payload, so a client cannot create an address on somebody else's account by guessing an
    id.
    """

    class Meta:
        model = Address
        fields = (
            "id",
            "full_name",
            "phone",
            "line1",
            "line2",
            "city",
            "region",
            "postal_code",
            "country",
            "address_type",
            "is_default_shipping",
            "is_default_billing",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_country(self, value: str) -> str:
        """Normalise to the upper-case ISO 3166-1 alpha-2 form the constraint requires."""
        code = (value or "").strip().upper()
        if len(code) != 2 or not code.isalpha():
            raise serializers.ValidationError("Use a two-letter country code, for example GB.")
        return code


class RegisterSerializer(serializers.Serializer):
    """Account creation over the API.

    Mirrors :class:`apps.accounts.forms.RegistrationForm` rather than replacing it, so the
    password policy stays in one place (``AUTH_PASSWORD_VALIDATORS``) for both surfaces.
    """

    email = serializers.EmailField(max_length=254)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, value: str) -> str:
        email = User.normalize_email_value(value)
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError(
                "An account with this email address already exists. Try signing in instead."
            )
        return email

    def validate_password(self, value: str) -> str:
        from django.contrib.auth import password_validation

        # ``None`` for the user: there is nothing to compare the password against yet, which is
        # the correct state for a brand-new account.
        password_validation.validate_password(value, None)
        return value

    def create(self, validated_data: dict) -> User:
        from apps.accounts.services import create_account

        return create_account(**validated_data)


class ChangePasswordSerializer(serializers.Serializer):
    """Password change for a signed-in API client."""

    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_current_password(self, value: str) -> str:
        request = self.context["request"]
        if not request.user.check_password(value):
            raise serializers.ValidationError("Your password is not correct.")
        return value

    def validate_new_password(self, value: str) -> str:
        from django.contrib.auth import password_validation

        password_validation.validate_password(value, self.context["request"].user)
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password", "updated_at"])
        return user
