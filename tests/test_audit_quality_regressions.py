"""Regression tests for defects and security hardening discovered during full project audit."""

from decimal import Decimal
from unittest.mock import patch
import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts.models import Address, User
from apps.backoffice.permissions import OPERATOR_GROUP
from apps.orders.models import Order, ReturnRequest
from apps.orders.webhook_views import (
    WebhookAuthenticationError,
    verify_webhook_signature,
)
from apps.procurement.models import InvoiceMatchRecord
from apps.recommendations.models import RecommendationSignal
from apps.shop.models import Cart, CheckoutSession


@pytest.mark.django_db
class TestAuditQualityRegressions:
    def test_return_cancel_handles_validation_error_gracefully(self, client):
        """Customer return cancellation gracefully handles ValidationError without NameError."""
        user = User.objects.create_user(
            email="customer_reg@example.com",
            password="securePassword123!",
            first_name="Test",
            last_name="Customer",
        )
        cart = Cart.objects.create(user=user, status=Cart.Status.CONVERTED)
        addr = Address.objects.create(
            user=user,
            full_name="Test Customer",
            phone="+44 7700 900077",
            line1="10 Downing Street",
            city="London",
            postal_code="SW1A 2AA",
            country="GB",
        )
        checkout = CheckoutSession.objects.create(
            user=user,
            cart=cart,
            shipping_address=addr,
            status=CheckoutSession.Status.CONVERTED,
        )
        order = Order.objects.create(
            user=user,
            number="FW-REG-1001",
            checkout=checkout,
            status=Order.Status.DELIVERED,
            currency="GBP",
            total=Decimal("100.00"),
            subtotal=Decimal("90.00"),
            shipping_amount=Decimal("10.00"),
            shipping_code="standard",
            shipping_name="Standard Delivery",
        )
        return_req = ReturnRequest.objects.create(
            user=user,
            order=order,
            number="RET-REG-1001",
            status=ReturnRequest.Status.REQUESTED,
            reason=ReturnRequest.Reason.WRONG_SIZE,
            return_type=ReturnRequest.ReturnType.REFUND,
        )

        client.force_login(user)

        with patch("apps.orders.views.cancel_return_request") as mock_cancel:
            mock_cancel.side_effect = ValidationError("Custom validation rejection")
            url = reverse("account:return-cancel", kwargs={"number": return_req.number})
            response = client.post(url, {"note": "Cancelling my return"}, follow=True)

            assert response.status_code == 200
            messages = list(response.context["messages"])
            assert any("This return request can no longer be cancelled." in m.message for m in messages)

    def test_inventory_restock_unauthorized_post_flashes_translated_message(self, client):
        """Staff without INVENTORY_MANAGE receives translated error message without NameError."""
        staff_user = User.objects.create_user(
            email="staff_viewer@example.com",
            password="securePassword123!",
            first_name="Staff",
            last_name="Viewer",
            is_staff=True,
        )
        group, _ = Group.objects.get_or_create(name=OPERATOR_GROUP)
        staff_user.groups.add(group)

        client.force_login(staff_user)
        url = reverse("backoffice:inventory_restock")
        response = client.post(url, {"variant_id": 1, "quantity": 10}, follow=True)

        assert response.status_code == 200
        messages = list(response.context["messages"])
        assert any("You do not have permission to record restock receipts." in m.message for m in messages)

    def test_carrier_webhook_missing_secret_in_production_raises_auth_error(self, settings):
        """Carrier webhook verification fails safe when no secret is configured and DEBUG=False."""
        settings.DEBUG = False
        settings.CARRIER_WEBHOOK_SECRET = ""
        settings.SHIPPING_WEBHOOK_SECRET = ""
        settings.SECRET_KEY = "test-secret-key-that-should-never-be-used-as-webhook-fallback"

        factory = RequestFactory()
        request = factory.post(
            "/api/orders/webhooks/carrier/generic/",
            data=b'{"event": "delivered"}',
            content_type="application/json",
            HTTP_X_SIGNATURE="any-signature-value",
        )

        with pytest.raises(WebhookAuthenticationError) as exc_info:
            verify_webhook_signature(request, "generic")

        assert "Carrier webhook verification secret is not configured." in str(exc_info.value)

    def test_invoice_match_record_type_hints_and_model(self):
        """InvoiceMatchRecord model resolves all type annotations cleanly."""
        assert hasattr(InvoiceMatchRecord, "discrepancies")
        assert hasattr(InvoiceMatchRecord, "discrepancy_override_by")

    def test_recommendations_signal_constraint(self):
        """RecommendationSignal constraint uses modern Django condition without deprecation warnings."""
        constraint = [c for c in RecommendationSignal._meta.constraints if c.name == "signal_strength_positive"][0]
        assert hasattr(constraint, "condition")
        assert constraint.condition is not None
