"""Closet API (Phase 8).

Read-only for authenticated users; each customer's own rows only.  Serializers enforce
``source``/``order_item``/``unit``/``variant`` as read-only so they cannot be set
from the API — those come from delivered orders or manual entry via HTML forms.

The namespace is ``v1`` (registered in ``config/api/v1/urls.py``).
"""

from __future__ import annotations

from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.closet.models import ClosetItem, Outfit


class ClosetItemSerializer(serializers.ModelSerializer):
    """Read-only (except upload) serializer for a wardrobe piece."""

    class Meta:
        model = ClosetItem
        fields = (
            "pk",
            "name",
            "category",
            "brand",
            "color",
            "size",
            "material",
            "season",
            "occasion",
            "style",
            "notes",
            "source",
            "order_item",
            "unit",
            "variant",
            "image",
            "status",
            "created_at",
        )
        read_only_fields = (
            "source",
            "order_item",
            "unit",
            "variant",
            "image",
        )


class OutfitSerializer(serializers.ModelSerializer):
    """Read-only serializer for an outfit."""

    class Meta:
        model = Outfit
        fields = (
            "pk",
            "name",
            "description",
            "season",
            "occasion",
            "style",
            "status",
            "created_at",
            "updated_at",
        )


class ClosetItemListView(APIView):
    """List the signed-in customer's closet items."""

    permission_classes = [IsAuthenticated]

    def get(self, request) -> Response:
        items = ClosetItem.objects.filter(user=request.user)
        return Response(ClosetItemSerializer(items, many=True).data)


class ClosetItemDetailView(APIView):
    """Retrieve one closet item (404 if it belongs to another user)."""

    permission_classes = [IsAuthenticated]

    def get_object(self, request, pk: int):
        from django.shortcuts import get_object_or_404

        return get_object_or_404(ClosetItem, pk=pk, user=request.user)

    def get(self, request, pk: int) -> Response:
        return Response(ClosetItemSerializer(self.get_object(request, pk)).data)


class OutfitListView(APIView):
    """List the signed-in customer's outfits."""

    permission_classes = [IsAuthenticated]

    def get(self, request) -> Response:
        outfits = Outfit.objects.filter(user=request.user)
        return Response(OutfitSerializer(outfits, many=True).data)


class OutfitDetailView(APIView):
    """Retrieve one outfit (404 if it belongs to another user)."""

    permission_classes = [IsAuthenticated]

    def get_object(self, request, pk: int):
        from django.shortcuts import get_object_or_404

        return get_object_or_404(Outfit, pk=pk, user=request.user)

    def get(self, request, pk: int) -> Response:
        return Response(OutfitSerializer(self.get_object(request, pk)).data)


class ApiRootView(APIView):
    """Machine-readable index of the endpoints exposed by API v1."""

    permission_classes = [IsAuthenticated]
    authentication_classes: list = []
    renderer_classes = []

    def get(self, request, *args, **kwargs):
        from django.urls import reverse

        def _reverse(name, request=request):
            return reverse(f"v1:{name}", request=request)

        return Response(
            {
                "service": "flashwear",
                "version": "v1",
                "endpoints": {
                    "health": _reverse("health"),
                    "products": _reverse("product-list"),
                    "categories": _reverse("category-list"),
                    "collections": _reverse("collection-list"),
                    "brands": _reverse("brand-list"),
                    "account_me": _reverse("account-me"),
                    "account_password": _reverse("account-password"),
                    "addresses": _reverse("address-list"),
                    "orders": _reverse("order-list"),
                    "closet_items": _reverse("closet-item-list"),
                    "closet_item": _reverse("closet-item-detail"),
                    "outfits": _reverse("outfit-list"),
                    "outfit": _reverse("outfit-detail"),
                    "checkout_promotion": _reverse("checkout-promotion"),
                },
            }
        )
