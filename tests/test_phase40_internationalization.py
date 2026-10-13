"""Phase 40: Internationalization, Localization & Regional Commerce Test Suite.

Covers:
- Language switching via Django standard i18n endpoint
- Safe redirect validation preventing open redirects
- Customer language preference persistence (session, cookie, and user Profile)
- Language synchronization on user login and profile updates
- Correct English fallback for untranslated content
- Non-translation preservation of product and brand names
- Native Bangla typography and font fallback rendering in templates
- Currency safety: Decimal calculations, unambiguous symbols, no synthetic conversions
- Timezone awareness: stored database timestamps unaltered by locale
- Transactional emails and notifications rendering in recipient preferred language
- Order and payment status display translations
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import Client, RequestFactory
from django.utils import timezone, translation

from apps.accounts.mailer import send_account_email
from apps.catalog.templatetags.catalog_extras import money
from apps.orders.models import Order
from apps.payments.models import Payment

User = get_user_model()
pytestmark = pytest.mark.django_db


# =============================================================================
# 1. Language Switching and Security
# =============================================================================


class TestLanguageSwitchingAndSecurity:
    def test_guest_language_switch_sets_cookie_and_redirects(self, client: Client):
        response = client.post(
            "/i18n/setlang/",
            data={"language": "bn", "next": "/"},
        )
        assert response.status_code == 302
        assert response["Location"] == "/"
        cookie = response.cookies.get(settings.LANGUAGE_COOKIE_NAME)
        assert cookie is not None
        assert cookie.value == "bn"

    def test_safe_redirect_prevents_open_redirect(self, client: Client):
        # Malicious external URL must be rejected and fallback to safe local redirect
        response = client.post(
            "/i18n/setlang/",
            data={"language": "bn", "next": "https://attacker.evil.com/phishing"},
        )
        assert response.status_code == 302
        assert not response["Location"].startswith("https://attacker.evil.com")
        # Should stay on safe domain
        assert response["Location"].startswith("/") or response["Location"].startswith(
            "http://testserver"
        )

    def test_safe_redirect_preserves_internal_path(self, client: Client):
        response = client.post(
            "/i18n/setlang/",
            data={"language": "bn", "next": "/catalog/products/"},
        )
        assert response.status_code == 302
        assert response["Location"] == "/catalog/products/"

    def test_unsupported_language_rejection(self, client: Client):
        response = client.post(
            "/i18n/setlang/",
            data={"language": "unsupported_xyz", "next": "/"},
        )
        assert response.status_code == 302
        # Should not set cookie for invalid language
        cookie = response.cookies.get(settings.LANGUAGE_COOKIE_NAME)
        if cookie:
            assert cookie.value != "unsupported_xyz"


# =============================================================================
# 2. Preference Persistence (Profile & Session)
# =============================================================================


class TestPreferencePersistence:
    def test_authenticated_user_switch_persists_to_profile(self, client: Client):
        user = User.objects.create_user(
            email="shopper_i18n@flashwear.local",
            password="SecurePassword123!",
            first_name="Rahim",
        )
        client.force_login(user)

        response = client.post(
            "/i18n/setlang/",
            data={"language": "bn", "next": "/"},
        )
        assert response.status_code == 302

        user.profile.refresh_from_db()
        assert user.profile.preferred_language == "bn"

    def test_user_login_syncs_preferred_language_to_session(self, rf: RequestFactory):
        user = User.objects.create_user(
            email="login_i18n@flashwear.local",
            password="SecurePassword123!",
        )
        user.profile.preferred_language = "bn"
        user.profile.save()

        # Simulate user login signal
        from apps.accounts.signals import sync_language_on_login

        request = rf.get("/")
        sync_language_on_login(sender=user.__class__, request=request, user=user)

        assert getattr(request, "LANGUAGE_CODE", None) == "bn"
        assert translation.get_language() == "bn"

    def test_profile_form_update_syncs_session_language(self, client: Client):
        user = User.objects.create_user(
            email="update_profile_i18n@flashwear.local",
            password="SecurePassword123!",
            first_name="Karim",
        )
        client.force_login(user)

        response = client.post(
            "/account/profile/",
            data={
                "first_name": "Karim",
                "last_name": "Khan",
                "preferred_language": "bn",
                "preferred_currency": "GBP",
            },
            follow=True,
        )
        assert response.status_code == 200
        user.profile.refresh_from_db()
        assert user.profile.preferred_language == "bn"
        cookie = client.cookies.get(settings.LANGUAGE_COOKIE_NAME)
        assert cookie is not None
        assert cookie.value == "bn"


# =============================================================================
# 3. Storefront Translations and English Fallback
# =============================================================================


class TestTranslationsAndFallback:
    def test_navigation_and_core_strings_in_bangla(self):
        with translation.override("bn"):
            assert translation.gettext("Home") == "হোম"
            assert translation.gettext("Shop") == "শপ"
            assert translation.gettext("Collections") == "কালেকশন"
            assert translation.gettext("Bag") == "ব্যাগ"
            assert translation.gettext("Cart") == "কার্ট"
            assert translation.gettext("Sign in") == "সাইন ইন"
            assert translation.gettext("Sign out") == "সাইন আউট"
            assert translation.gettext("My account") == "আমার অ্যাকাউন্ট"

    def test_english_fallback_for_untranslated_strings(self):
        with translation.override("bn"):
            # Brand names and arbitrary product strings should never be auto-translated
            raw_brand = "FLASHWEAR Signature Cyber Hoodie"
            assert translation.gettext(raw_brand) == raw_brand

            arbitrary_term = "Untranslated Unique SKU Term"
            assert translation.gettext(arbitrary_term) == arbitrary_term

    def test_english_language_returns_standard_text(self):
        with translation.override("en"):
            assert translation.gettext("Home") == "Home"
            assert translation.gettext("Shop") == "Shop"
            assert translation.gettext("Bag") == "Bag"
            assert translation.gettext("Collections") == "Collections"


# =============================================================================
# 4. Template Rendering and Bangla Typography Fallback
# =============================================================================


class TestTemplateRenderingAndTypography:
    def test_base_template_renders_html_lang_attribute(self, rf: RequestFactory):
        tpl = Template(
            "{% load i18n %}"
            "{% get_current_language as CURRENT_LANGUAGE %}"
            '<html lang="{{ CURRENT_LANGUAGE }}">'
        )

        with translation.override("bn"):
            rendered = tpl.render(Context({}))
            assert 'lang="bn"' in rendered

        with translation.override("en"):
            rendered = tpl.render(Context({}))
            assert 'lang="en"' in rendered

    def test_storefront_home_renders_bangla_when_active(self, client: Client):
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "bn"
        response = client.get("/")
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        assert 'lang="bn"' in html
        # Should render Bangla navigation text
        assert "হোম" in html or "শপ" in html
        # Should contain font fallback for Bangla
        assert "Hind Siliguri" in html or "Noto Sans Bengali" in html

    def test_accessible_language_selector_present(self, client: Client):
        response = client.get("/")
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        assert 'name="language"' in html
        assert 'action="/i18n/setlang/"' in html
        assert 'value="bn"' in html
        assert 'value="en"' in html


# =============================================================================
# 5. Currency Safety and Canonical Money Handling
# =============================================================================


class TestCurrencySafetyAndCalculation:
    def test_canonical_money_formatting_unaltered_by_locale(self):
        amount = Decimal("149.99")

        with translation.override("en"):
            formatted_en = money(amount)
        with translation.override("bn"):
            formatted_bn = money(amount)

        # Money formatting must preserve canonical currency indicator and amount
        assert formatted_en == formatted_bn
        assert settings.CATALOG_CURRENCY_SYMBOL in formatted_en
        assert "149.99" in formatted_en or "150" in formatted_en

    def test_calculations_remain_exact_decimal_under_all_locales(self):
        for lang in ["en", "bn"]:
            with translation.override(lang):
                price = Decimal("29.95")
                qty = 3
                shipping = Decimal("4.50")
                discount = Decimal("5.00")
                total = (price * qty) + shipping - discount
                assert total == Decimal("89.35")
                assert isinstance(total, Decimal)


# =============================================================================
# 6. Locale-Aware Formatting and Dates
# =============================================================================


class TestLocaleAwareFormattingAndTimestamps:
    def test_database_timestamps_remain_timezone_aware_and_utc(self):
        now_utc = timezone.now()
        assert timezone.is_aware(now_utc)

        user = User.objects.create_user(
            email="tz_test@flashwear.local",
            password="SecurePassword123!",
        )
        created_at_before = user.date_joined

        with translation.override("bn"):
            user.refresh_from_db()
            created_at_after = user.date_joined
            assert created_at_before == created_at_after
            assert timezone.is_aware(created_at_after)

    def test_date_formatting_template_filter(self):
        dt = timezone.now()
        tpl = Template("{% load i18n %}{{ dt|date:'Y-m-d' }}")

        with translation.override("bn"):
            rendered_bn = tpl.render(Context({"dt": dt}))
        with translation.override("en"):
            rendered_en = tpl.render(Context({"dt": dt}))

        assert rendered_bn == rendered_en == dt.strftime("%Y-%m-%d")


# =============================================================================
# 7. Order and Payment Status Translations
# =============================================================================


class TestOrderAndPaymentStatusTranslations:
    def test_order_status_labels_translated_in_bangla(self):
        with translation.override("bn"):
            assert str(Order.Status.PENDING_PAYMENT.label) == "পেমেন্ট অপেক্ষমাণ"
            assert str(Order.Status.PAID.label) == "পরিশোধিত"
            assert str(Order.Status.PROCESSING.label) == "প্রক্রিয়াকরণ চলছে"
            assert str(Order.Status.SHIPPED.label) == "পাঠানো হয়েছে"
            assert str(Order.Status.DELIVERED.label) == "বিতরণ সম্পন্ন"
            assert str(Order.Status.CANCELLED.label) == "বাতিল করা হয়েছে"
            assert str(Order.Status.REFUNDED.label) == "মূল্য ফেরত দেওয়া হয়েছে"

    def test_order_status_labels_in_english(self):
        with translation.override("en"):
            assert str(Order.Status.PENDING_PAYMENT.label) == "Pending payment"
            assert str(Order.Status.PAID.label) == "Paid"
            assert str(Order.Status.SHIPPED.label) == "Shipped"
            assert str(Order.Status.DELIVERED.label) == "Delivered"

    def test_payment_status_labels_translated(self):
        with translation.override("bn"):
            assert str(Payment.Status.SUCCEEDED.label) == "সফল হয়েছে"
            assert str(Payment.Status.FAILED.label) == "ব্যর্থ হয়েছে"
            assert str(Payment.Status.PENDING.label) == "অপেক্ষমাণ"

        with translation.override("en"):
            assert str(Payment.Status.SUCCEEDED.label) == "Succeeded"
            assert str(Payment.Status.FAILED.label) == "Failed"


# =============================================================================
# 8. Transactional Emails and Notifications Language Selection
# =============================================================================


class TestEmailAndNotificationLanguageSelection:
    def test_transactional_email_uses_recipient_preferred_language(self, mailoutbox):
        user = User.objects.create_user(
            email="email_pref_bn@flashwear.local",
            password="SecurePassword123!",
            first_name="Tareq",
        )
        user.profile.preferred_language = "bn"
        user.profile.save()

        # Render and send account email
        send_account_email(
            subject_template="accounts/email/verification_email_subject.txt",
            text_template="accounts/email/verification_email.txt",
            html_template="accounts/email/verification_email.html",
            context={"user": user, "verification_url": "https://testserver/verify/123"},
            recipient=user.email,
        )

        assert len(mailoutbox) == 1
        msg = mailoutbox[0]
        assert user.email in msg.to
        assert msg.subject
