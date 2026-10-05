"""Account API views.

Session-authenticated, which is the right shape for the Phase 2 web client: the browser already
has a signed-in session, so it does not need a second credential. A native mobile client will want
token auth here; that is a deliberate later change, not an omission.

Every endpoint that touches a customer-owned object scopes it through the signed-in customer, so
one customer can never read or write another's data by changing an id in the URL.
"""

from __future__ import annotations

from django.contrib.auth import login
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import services
from apps.accounts.models import Address
from apps.accounts.serializers import (
    AddressSerializer,
    ChangePasswordSerializer,
    RegisterSerializer,
    UserSerializer,
)


class AccountMeView(APIView):
    """Read the signed-in customer's own record."""

    permission_classes = [IsAuthenticated]

    @method_decorator(never_cache)
    def get(self, request) -> Response:
        return Response(UserSerializer(request.user).data)


class RegisterView(APIView):
    """Create an account and return the same payload the web UI produces.

    The customer is signed in on success, so the response is a usable session rather than a
    credential the client has to store.
    """

    # Explicit rather than inherited: an empty list would fall back to whatever
    # ``DEFAULT_PERMISSION_CLASSES`` says, and "is registration open?" must never depend on that.
    permission_classes = [AllowAny]
    throttle_scope = "sensitive"

    def post(self, request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        services.send_verification_email(user, request)
        login(request, user, backend="apps.accounts.backends.EmailBackend")
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class ChangePasswordView(APIView):
    """Change the signed-in customer's password."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "sensitive"

    def post(self, request) -> Response:
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # The session was authenticated against the old password hash, so re-key it here rather
        # than silently signing the client out on its next request.
        from django.contrib.auth import update_session_auth_hash

        update_session_auth_hash(request, request.user)
        services.record_account_event(
            request.user, "password_changed", request=request, metadata={"via": "api"}
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AddressListCreateView(APIView):
    """List and create the signed-in customer's addresses."""

    permission_classes = [IsAuthenticated]

    def get(self, request) -> Response:
        addresses = request.user.addresses.all()
        return Response(AddressSerializer(addresses, many=True).data)

    def post(self, request) -> Response:
        serializer = AddressSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Owner from the request, not the payload.
        address = serializer.save(user=request.user)
        services.record_account_event(
            request.user,
            "address_created",
            request=request,
            metadata={"address_id": address.pk, "default": address.is_default_shipping},
        )
        return Response(AddressSerializer(address).data, status=status.HTTP_201_CREATED)


class AddressDetailView(APIView):
    """Read, update and delete one address, scoped to its owner."""

    permission_classes = [IsAuthenticated]

    def get_object(self, request, pk: int) -> Address:
        # A row belonging to somebody else is a 404, not a 403: a 403 would confirm the id exists.
        from django.shortcuts import get_object_or_404

        return get_object_or_404(Address, pk=pk, user=request.user)

    def get(self, request, pk: int) -> Response:
        return Response(AddressSerializer(self.get_object(request, pk)).data)

    def patch(self, request, pk: int) -> Response:
        address = self.get_object(request, pk)
        serializer = AddressSerializer(address, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        services.record_account_event(
            request.user,
            "address_updated",
            request=request,
            metadata={"address_id": address.pk},
        )
        return Response(serializer.data)

    def delete(self, request, pk: int) -> Response:
        address = self.get_object(request, pk)
        was_default = address.is_default_shipping
        address.delete()
        services.record_account_event(
            request.user,
            "address_deleted",
            request=request,
            metadata={"was_default": was_default},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
