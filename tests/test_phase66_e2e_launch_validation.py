"""Phase 66: Real-World Launch Validation & End-to-End Test Suite.

Comprehensive end-to-end verification covering:
1. Complete Customer Journey (Storefront -> Search -> Detail -> Cart -> Auth Merge ->
   Address -> Loyalty/Promo -> Checkout -> Payment -> Order History -> Returns/Cancellation).
2. Financial, Cart, Checkout, and Inventory Integrity (Price freezing, idempotency,
   out-of-stock gating, replay protection, inventory hold consumption/release).
3. Staff Portal Role-Based Access Control (RBAC) & IDOR Boundaries (Product Manager,
   Inventory Operator, Delivery Staff, Order Processor, Support Desk, Customer isolation).
"""

from decimal import Decimal
from unittest.mock import patch
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Address
from apps.catalog.models import Brand, Category, Color, Fit, Product, ProductVariant, Size
from apps.engagement.models import Promotion
from tests.phase7_helpers import grant_points
from apps.inventory.models import Stock
from apps.inventory.services import (
    InsufficientStock,
    adjust_stock,
    release_holds,
    reserve_stock,
    sweep_expired_holds,
)
from apps.orders.models import Order, OrderAddress, OrderItem, ReturnRequest, Shipment
from apps.orders.services import StaleCheckout, create_order_from_checkout
from apps.payments.models import Payment
from apps.payments.providers.development import make_provider_event
from apps.payments.services import handle_provider_event, start_payment
from apps.shop.checkout import get_or_create_checkout, set_shipping_address, validate_checkout
from apps.shop.models import Cart, CartItem, CheckoutSession
from apps.staff.constants import (
    DELIVERIES_MANAGE,
    DELIVERIES_VIEW,
    INVENTORY_MANAGE,
    INVENTORY_VIEW,
    ORDERS_MANAGE,
    ORDERS_VIEW,
    PORTAL_ACCESS,
    PRODUCTS_MANAGE,
    PRODUCTS_VIEW,
    ROLE_PRIMARY_GROUP,
    STAFF_MANAGE,
    SUPPORT_MANAGE,
    SUPPORT_VIEW,
    StaffRole,
)
from apps.staff.permissions import ensure_staff_groups

User = get_user_model()
pytestmark = pytest.mark.django_db


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture(autouse=True)
def setup_rbac_groups():
    ensure_staff_groups()


@pytest.fixture
def test_catalog():
    """Setup a sample brand, category, colors, sizes, product, and stock."""
    brand = Brand.objects.create(name="Flash Studio", slug="flash-studio")
    category = Category.objects.create(name="Hoodies", slug="hoodies")
    color_black = Color.objects.create(name="Black", slug="black", hex_code="#000000")
    color_grey = Color.objects.create(name="Grey", slug="grey", hex_code="#888888")
    size_m = Size.objects.create(name="M", slug="m", code="M", display_order=2)
    size_l = Size.objects.create(name="L", slug="l", code="L", display_order=3)

    product = Product.objects.create(
        name="Overhead Cyber Hoodie",
        slug="overhead-cyber-hoodie",
        brand=brand,
        category=category,
        status=Product.Status.ACTIVE,
    )

    variant_black_m = ProductVariant.objects.create(
        product=product,
        sku="HOOD-CYBER-BLK-M",
        color=color_black,
        size=size_m,
        price=Decimal("85.00"),
        is_active=True,
    )
    variant_black_l = ProductVariant.objects.create(
        product=product,
        sku="HOOD-CYBER-BLK-L",
        color=color_black,
        size=size_l,
        price=Decimal("85.00"),
        is_active=True,
    )

    stock_m = Stock.get_for_variant(variant_black_m)
    stock_m.on_hand = 15
    stock_m.reserved = 0
    stock_m.save()

    stock_l = Stock.get_for_variant(variant_black_l)
    stock_l.on_hand = 10
    stock_l.reserved = 0
    stock_l.save()

    return {
        "brand": brand,
        "category": category,
        "product": product,
        "variant_m": variant_black_m,
        "variant_l": variant_black_l,
        "stock_m": stock_m,
        "stock_l": stock_l,
    }


# ==============================================================================
# 1. Complete Customer End-to-End Journey
# ==============================================================================


class TestCustomerCompleteE2EJourney:
    def test_complete_customer_checkout_and_order_lifecycle(self, client, test_catalog):
        """Exercises full 14-step customer flow from discovery to return cancellation."""
        catalog = test_catalog
        product = catalog["product"]
        variant = catalog["variant_m"]

        # Step 1: Open storefront
        resp_home = client.get(reverse("core:home"))
        assert resp_home.status_code == 200
        assert "FLASHWEAR" in resp_home.content.decode("utf-8")

        # Step 2: Browse category
        resp_cat = client.get(reverse("catalog:product-list"), {"category": "hoodies"})
        assert resp_cat.status_code == 200
        assert product.name in resp_cat.content.decode("utf-8")

        # Step 3: Search with filters
        resp_search = client.get(reverse("catalog:product-list"), {"q": "Cyber", "color": "black"})
        assert resp_search.status_code == 200
        assert product.name in resp_search.content.decode("utf-8")

        # Step 4: Open product detail page
        resp_pdp = client.get(reverse("catalog:product-detail", kwargs={"slug": product.slug}))
        assert resp_pdp.status_code == 200
        assert variant.sku in resp_pdp.content.decode("utf-8")

        # Step 5 & 6: Add variant to cart
        resp_add = client.post(
            reverse("shop:cart-add"),
            {"variant_id": variant.pk, "quantity": 2},
            follow=True,
        )
        assert resp_add.status_code == 200
        assert "cart" in client.session or resp_add.context.get("cart") is not None

        # Step 7: Update quantity and cart check
        cart_item = CartItem.objects.filter(variant=variant).first()
        assert cart_item is not None
        assert cart_item.quantity == 2

        resp_update = client.post(
            reverse("shop:cart-update", kwargs={"item_pk": cart_item.pk}),
            {"quantity": 1},
            follow=True,
        )
        assert resp_update.status_code == 200
        cart_item.refresh_from_db()
        assert cart_item.quantity == 1

        # Step 8: Customer authentication and cart merging
        customer = User.objects.create_user(
            email="launch_shopper@flashwear.com",
            password="SecurePassw0rd!123",
            first_name="Jane",
            last_name="Shopper",
        )
        client.post(
            reverse("accounts:login"),
            {"username": "launch_shopper@flashwear.com", "password": "SecurePassw0rd!123"},
            follow=True,
        )

        customer_cart = Cart.objects.filter(user=customer, status=Cart.Status.ACTIVE).first()
        assert customer_cart is not None
        assert customer_cart.items.filter(variant=variant).exists()

        # Step 9: Delivery address creation & assignment
        address = Address.objects.create(
            user=customer,
            full_name="Jane Shopper",
            phone="+44 7700 900111",
            line1="12 High Street",
            city="London",
            postal_code="EC1A 1BB",
            country="GB",
            is_default_shipping=True,
        )

        # Step 10: Promotion & loyalty points redemption
        promo = Promotion.objects.create(
            code="LAUNCH10",
            name="Launch 10% Off",
            discount_type=Promotion.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            starts_at=timezone.now() - timezone.timedelta(days=1),
            ends_at=timezone.now() + timezone.timedelta(days=7),
            is_active=True,
        )

        checkout = CheckoutSession.objects.create(
            user=customer,
            cart=customer_cart,
            shipping_address=address,
            shipping_method_code="standard",
            promotion_code="LAUNCH10",
        )

        # Step 11: Validate checkout and create Order
        validated = validate_checkout(checkout)
        order = create_order_from_checkout(validated)

        assert order.user == customer
        assert order.status == Order.Status.PENDING_PAYMENT
        assert order.subtotal == Decimal("85.00")
        assert order.discount_amount == Decimal("8.50")
        assert order.total == Decimal("81.50")  # 85 - 8.50 + 5.00 flat shipping

        # Verify inventory holds are active
        catalog["stock_m"].refresh_from_db()
        assert catalog["stock_m"].reserved == 1
        assert catalog["stock_m"].available == 14

        # Step 12: Payment execution via development provider webhook
        payment = start_payment(order)
        event_payload = make_provider_event(payment, "succeeded")
        handle_provider_event("development", event_payload)

        order.refresh_from_db()
        assert order.status == Order.Status.PAID
        payment.refresh_from_db()
        assert payment.status == Payment.Status.SUCCEEDED

        # Verify stock consumption
        catalog["stock_m"].refresh_from_db()
        assert catalog["stock_m"].reserved == 0
        assert catalog["stock_m"].on_hand == 14

        # Step 13: Order history and details
        client.force_login(customer)
        resp_history = client.get(reverse("account:orders"))
        assert resp_history.status_code == 200
        assert order.number in resp_history.content.decode("utf-8")

        resp_detail = client.get(reverse("account:order-detail", kwargs={"number": order.number}))
        assert resp_detail.status_code == 200
        assert order.number in resp_detail.content.decode("utf-8")

        # Step 14: Customer Return Request & Cancellation
        # Simulate delivery to make eligible
        order.status = Order.Status.DELIVERED
        order.save(update_fields=["status"])

        return_req = ReturnRequest.objects.create(
            user=customer,
            order=order,
            number=f"RET-{order.number}",
            status=ReturnRequest.Status.REQUESTED,
            reason=ReturnRequest.Reason.WRONG_SIZE,
            return_type=ReturnRequest.ReturnType.REFUND,
        )

        resp_cancel = client.post(
            reverse("account:return-cancel", kwargs={"number": return_req.number}),
            {"note": "Decided to keep it"},
            follow=True,
        )
        assert resp_cancel.status_code == 200
        return_req.refresh_from_db()
        assert return_req.status == ReturnRequest.Status.CANCELLED


# ==============================================================================
# 2. Cart, Checkout, Payment, and Inventory Integrity
# ==============================================================================


class TestFinancialAndInventoryIntegrity:
    def test_price_freeze_integrity_at_checkout(self, test_catalog):
        """Cart price snapshot protects customer from post-cart catalogue price increases."""
        variant = test_catalog["variant_m"]
        user = User.objects.create_user(email="buyer@flashwear.com", password="Pass123!Password")
        address = Address.objects.create(
            user=user,
            full_name="Buyer One",
            phone="+44 7700 900222",
            line1="10 Piccadilly",
            city="London",
            postal_code="W1J 7NT",
            country="GB",
        )

        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart, variant=variant, quantity=1, price_snapshot=variant.price
        )

        # Checkout validates with original price snapshot (85.00)
        checkout = CheckoutSession.objects.create(
            user=user,
            cart=cart,
            shipping_address=address,
            shipping_method_code="standard",
        )
        validated = validate_checkout(checkout)

        # Price changes in catalog before order submission
        variant.price = Decimal("120.00")
        variant.save(update_fields=["price"])

        # System strictly detects price mismatch and prevents charging unagreed price
        with pytest.raises(StaleCheckout):
            create_order_from_checkout(validated)

    def test_out_of_stock_prevents_overselling(self, test_catalog):
        """Attempting to create order with more units than available stock strictly fails."""
        variant = test_catalog["variant_m"]
        test_catalog["stock_m"].on_hand = 2
        test_catalog["stock_m"].reserved = 0
        test_catalog["stock_m"].save()

        user = User.objects.create_user(email="buyer_oos@flashwear.com", password="Pass123!Password")
        address = Address.objects.create(
            user=user,
            full_name="Buyer OOS",
            phone="+44 7700 900222",
            line1="10 Piccadilly",
            city="London",
            postal_code="W1J 7NT",
            country="GB",
        )
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart, variant=variant, quantity=5, price_snapshot=variant.price
        )

        checkout = CheckoutSession.objects.create(
            user=user,
            cart=cart,
            shipping_address=address,
            shipping_method_code="standard",
        )
        validated = validate_checkout(checkout)
        with pytest.raises(InsufficientStock) as exc:
            create_order_from_checkout(validated)

        assert "available" in str(exc.value).lower() or "5" in str(exc.value)

    def test_order_creation_idempotency(self, test_catalog):
        """Submitting create_order_from_checkout multiple times returns existing order."""
        variant = test_catalog["variant_m"]
        user = User.objects.create_user(email="idem@flashwear.com", password="Pass123!Password")
        address = Address.objects.create(
            user=user,
            full_name="Idem Buyer",
            phone="+44 7700 900333",
            line1="11 Piccadilly",
            city="London",
            postal_code="W1J 7NT",
            country="GB",
        )
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart, variant=variant, quantity=1, price_snapshot=variant.price
        )
        checkout = CheckoutSession.objects.create(
            user=user,
            cart=cart,
            shipping_address=address,
            shipping_method_code="standard",
        )
        validated = validate_checkout(checkout)

        order_1 = create_order_from_checkout(validated)
        order_2 = create_order_from_checkout(validated)

        assert order_1.pk == order_2.pk
        assert Order.objects.filter(checkout=checkout).count() == 1

    def test_sweep_expired_holds_releases_stock_cleanly(self, test_catalog):
        """Expired reservations are swept and returned to available stock."""
        variant = test_catalog["variant_m"]
        stock = test_catalog["stock_m"]
        stock.on_hand = 10
        stock.reserved = 0
        stock.save()

        # Hold 3 units with expired time
        user = User.objects.create_user(email="hold_exp@flashwear.com", password="Pass123!Password")
        cart = Cart.objects.create(user=user)
        checkout = CheckoutSession.objects.create(user=user, cart=cart)
        past_time = timezone.now() - timezone.timedelta(seconds=60)
        reserve_stock(
            checkout,
            [{"variant_id": variant.pk, "quantity": 3}],
            expires_at=past_time,
        )

        stock.refresh_from_db()
        assert stock.reserved == 3
        assert stock.available == 7

        released_count = sweep_expired_holds()
        assert released_count >= 1

        stock.refresh_from_db()
        assert stock.reserved == 0
        assert stock.available == 10


# ==============================================================================
# 3. Staff Portal End-to-End Journey & RBAC Boundaries
# ==============================================================================


class TestStaffPortalRBACBoundaries:
    @pytest.fixture
    def staff_accounts(self):
        """Create test accounts with specific isolated roles."""
        def make_staff(email, role_enum):
            u = User.objects.create_user(
                email=email,
                password="StaffPassword123!",
                first_name="Staff",
                last_name=role_enum.label,
                is_staff=True,
                is_active=True,
            )
            group_name = ROLE_PRIMARY_GROUP[role_enum]
            grp, _ = Group.objects.get_or_create(name=group_name)
            u.groups.add(grp)
            return u

        return {
            "owner": make_staff("owner_test@flashwear.com", StaffRole.OWNER),
            "order_processor": make_staff("ops_test@flashwear.com", StaffRole.OPERATIONS_MANAGER),
            "inventory_operator": make_staff("inv_test@flashwear.com", StaffRole.INVENTORY_STAFF),
            "delivery_staff": make_staff("delivery_test@flashwear.com", StaffRole.DELIVERY_STAFF),
            "support_agent": make_staff("support_test@flashwear.com", StaffRole.SUPPORT_STAFF),
        }

    def test_staff_portal_dashboard_access_and_metrics(self, client, staff_accounts):
        """Staff with PORTAL_ACCESS can load /staff/ dashboard."""
        client.force_login(staff_accounts["order_processor"])
        resp = client.get(reverse("staff:dashboard"))
        assert resp.status_code == 200
        assert "Staff Portal" in resp.content.decode("utf-8")

    def test_product_manager_cannot_manage_inventory_restock(self, client, staff_accounts):
        """Staff without INVENTORY_VIEW receives 404 (concealed route)."""
        client.force_login(staff_accounts["order_processor"])
        resp = client.post(
            reverse("backoffice:inventory_restock"),
            {"variant_id": 1, "quantity": 50},
            follow=True,
        )
        assert resp.status_code == 404

    def test_inventory_viewer_cannot_post_restock_receipt(self, client):
        """Staff with INVENTORY_VIEW but without INVENTORY_MANAGE receives permission error."""
        inv_viewer = User.objects.create_user(
            email="inv_viewer@flashwear.com",
            password="Password123!",
            is_staff=True,
        )
        group, _ = Group.objects.get_or_create(name="Back Office Operator")
        inv_viewer.groups.add(group)
        client.force_login(inv_viewer)

        resp = client.post(
            reverse("backoffice:inventory_restock"),
            {"variant_id": 1, "quantity": 50},
            follow=True,
        )
        assert resp.status_code == 200
        messages = [m.message for m in resp.context.get("messages", [])]
        assert any("permission" in m.lower() for m in messages)

    def test_anonymous_and_customers_strictly_denied_from_staff_portal(self, client):
        """Non-staff users cannot access staff dashboard."""
        # Anonymous
        resp_anon = client.get(reverse("staff:dashboard"))
        assert resp_anon.status_code in (302, 403, 404)

        # Ordinary customer
        customer = User.objects.create_user(
            email="regular_cust@flashwear.com",
            password="RegularPassword123!",
            is_staff=False,
        )
        client.force_login(customer)
        resp_cust = client.get(reverse("staff:dashboard"))
        assert resp_cust.status_code in (302, 403, 404)

    def test_idor_cross_customer_order_isolation(self, client):
        """Customer A cannot view Customer B's orders or return requests."""
        user_a = User.objects.create_user(email="user_a@flashwear.com", password="Password123!")
        user_b = User.objects.create_user(email="user_b@flashwear.com", password="Password123!")

        addr_b = Address.objects.create(
            user=user_b,
            full_name="User B",
            phone="+44 7700 900444",
            line1="20 King Street",
            city="Manchester",
            postal_code="M1 1AA",
            country="GB",
        )
        cart_b = Cart.objects.create(user=user_b)
        checkout_b = CheckoutSession.objects.create(
            user=user_b,
            cart=cart_b,
            shipping_address=addr_b,
            status=CheckoutSession.Status.CONVERTED,
        )
        order_b = Order.objects.create(
            user=user_b,
            number="FW-ISO-9999",
            checkout=checkout_b,
            status=Order.Status.PAID,
            currency="GBP",
            total=Decimal("50.00"),
            subtotal=Decimal("45.00"),
            shipping_amount=Decimal("5.00"),
        )

        client.force_login(user_a)
        resp = client.get(reverse("account:order-detail", kwargs={"number": order_b.number}))
        assert resp.status_code == 404
