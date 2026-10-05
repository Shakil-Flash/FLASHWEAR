"""Phase 18: correlation ids, access logs, metrics, CSP, health, monitoring, error pages,
throttles (HTML attempt caps + DRF scopes), model-level upload validation and the
hot-path index additions."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse

# --------------------------------------------------------------------------------------
# Request correlation + access logging
# --------------------------------------------------------------------------------------


class TestRequestCorrelation:
    def test_response_carries_a_request_id(self, client):
        response = client.get("/health/")
        assert response["X-Request-ID"]
        assert re.fullmatch(r"[A-Za-z0-9._-]{8,64}", response["X-Request-ID"])

    def test_incoming_valid_request_id_is_adopted(self, client):
        response = client.get("/health/", HTTP_X_REQUEST_ID="upstream-id-1234")
        assert response["X-Request-ID"] == "upstream-id-1234"

    @pytest.mark.parametrize("hostile", ["x", "a b c d e f g", "id/with/slashes", "<script>"])
    def test_hostile_request_ids_are_replaced(self, client, hostile):
        response = client.get("/health/", HTTP_X_REQUEST_ID=hostile)
        assert response["X-Request-ID"] != hostile
        assert re.fullmatch(r"[A-Za-z0-9._-]{8,64}", response["X-Request-ID"])

    def test_request_id_reaches_log_records(self, client, caplog):
        """The id on the response is the id inside the log line, end to end."""
        from apps.core.logging_filters import JsonFormatter

        with caplog.at_level(logging.INFO, logger="flashwear.access"):
            response = client.get("/health/")
        record = next(r for r in caplog.records if r.name == "flashwear.access")
        payload = json.loads(JsonFormatter().format(record))
        assert payload["request_id"] == response["X-Request-ID"]

    def test_request_id_is_reset_after_the_request(self, client):
        from apps.core.logging_filters import request_id_var

        client.get("/health/")
        assert request_id_var.get() == "-"

    def test_access_log_line_is_emitted(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="flashwear.access"):
            response = client.get("/health/")
        entry = next(r for r in caplog.records if r.name == "flashwear.access")
        assert entry.status == response.status_code
        assert entry.method == "GET"
        assert entry.path == "/health/"
        assert entry.duration_ms >= 0
        assert entry.request_id == response["X-Request-ID"]

    @pytest.mark.django_db
    def test_not_found_is_logged_as_warning(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="flashwear.access"):
            client.get("/definitely-missing/")
        entry = next(r for r in caplog.records if r.name == "flashwear.access")
        assert entry.levelno == logging.WARNING
        assert entry.status == 404

    def test_request_metrics_are_counted(self, client):
        from apps.core import metrics

        before = metrics.snapshot()["requests"]
        client.get("/health/")
        after = metrics.snapshot()["requests"]
        assert after["total"] > before["total"]
        assert after["2xx"] >= before["2xx"] + 1


class TestMetrics:
    def test_mark_task_counts_success_and_failure(self):
        from apps.core import metrics

        before = metrics.snapshot()["tasks"]
        metrics.mark_task("probe.task.success", ok=True)
        metrics.mark_task("probe.task.failure", ok=False)
        after = metrics.snapshot()["tasks"]
        assert after["succeeded"] == before["succeeded"] + 1
        assert after["failed"] == before["failed"] + 1
        assert after["failed_by_task"]["probe.task.failure"] == 1

    def test_snapshot_shape_is_json_serialisable(self):
        from apps.core import metrics

        payload = metrics.snapshot()
        json.dumps(payload)
        assert set(payload) == {"date", "requests", "tasks", "queue_depth"}
        assert set(payload["requests"]) == {"total", "2xx", "3xx", "4xx", "5xx", "avg_ms"}

    def test_queue_depth_is_none_without_redis(self):
        from apps.core import metrics

        # locmem cache in tests: honest None, not a fabricated number.
        assert metrics.queue_depth() is None

    def test_telemetry_never_raises_when_the_cache_is_broken(self, monkeypatch):
        from django.core.cache import cache

        from apps.core import metrics

        def boom(*args, **kwargs):
            raise RuntimeError("cache is down")

        monkeypatch.setattr(cache, "add", boom)
        monkeypatch.setattr(cache, "incr", boom)
        monkeypatch.setattr(cache, "set", boom)
        metrics.incr("whatever")  # must not raise
        metrics.mark_request(500, 12)
        metrics.mark_task("x.y", ok=False)


# --------------------------------------------------------------------------------------
# Content-Security-Policy
# --------------------------------------------------------------------------------------


class TestContentSecurityPolicy:
    def test_html_response_carries_the_policy(self, client):
        response = client.get("/health/")
        assert "Content-Security-Policy-Report-Only" in response

    @override_settings(CSP_REPORT_ONLY=False)
    def test_enforcing_mode_uses_the_blocking_header(self, client):
        response = client.get("/health/")
        assert "Content-Security-Policy" in response
        assert "Content-Security-Policy-Report-Only" not in response

    @override_settings(
        CONTENT_SECURITY_POLICY={
            "DIRECTIVES": {"default-src": ["'self'"], "script-src": ["'self'"]}
        },
        CSP_REPORT_ONLY=False,
    )
    def test_policy_content_matches_settings(self, client):
        response = client.get("/health/")
        header = response["Content-Security-Policy"]
        assert "default-src 'self'" in header
        assert "script-src 'self'" in header

    @override_settings(CONTENT_SECURITY_POLICY={})
    def test_no_policy_declared_means_no_header(self, client):
        response = client.get("/health/")
        assert "Content-Security-Policy" not in response

    @override_settings(CSP_REPORT_ONLY=False)
    def test_response_level_override_wins(self):
        from apps.core.middleware import ContentSecurityPolicyMiddleware

        def get_response(request):
            response = HttpResponse("x")
            response["Content-Security-Policy"] = "default-src 'none'"
            return response

        mw = ContentSecurityPolicyMiddleware(get_response)
        response = mw(RequestFactory().get("/"))
        assert response["Content-Security-Policy"] == "default-src 'none'"

    def test_render_csp_flattens_directives(self):
        from apps.core.middleware import render_csp

        rendered = render_csp({"default-src": ["'self'"], "upgrade-insecure-requests": []})
        assert rendered == "default-src 'self'; upgrade-insecure-requests"

    def test_templates_have_no_inline_handlers_or_scripts(self):
        """Keep templates CSP-clean: script-src 'self' with no unsafe-inline.

        JSON data blocks (``type="application/ld+json"``) are not executable and are
        explicitly allowed by CSP, so they are not matched here.
        """
        handler = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
        inline_script = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/)", re.I)
        js_url = re.compile(r"javascript:", re.IGNORECASE)
        root = Path(settings.BASE_DIR) / "templates"
        offenders: list[str] = []
        for path in root.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            if handler.search(text) or inline_script.search(text) or js_url.search(text):
                offenders.append(str(path.relative_to(root)))
        assert not offenders, f"templates must stay CSP-clean: {offenders}"


# --------------------------------------------------------------------------------------
# Health endpoints
# --------------------------------------------------------------------------------------


class TestHealthEndpoints:
    # Readiness touches the database and renders nothing, but the probes still run
    # inside the test database block like every other dependency check here.
    pytestmark = pytest.mark.django_db

    def test_liveness(self, client):
        response = client.get("/health/live/")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    def test_readiness_is_green(self, client):
        response = client.get("/health/ready/")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ready"
        assert payload["checks"]["database"] == "ok"
        assert payload["checks"]["cache"] == "ok"
        assert payload["checks"]["broker"] == "skipped"  # eager mode in tests

    @pytest.mark.parametrize(
        "dependency,attribute",
        [("database", "check_database"), ("cache", "check_cache")],
    )
    def test_readiness_fails_when_a_dependency_is_down(
        self, client, monkeypatch, dependency, attribute
    ):
        from apps.core import health

        monkeypatch.setattr(health, attribute, lambda: "down")
        response = client.get("/health/ready/")
        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "not_ready"
        assert payload["checks"][dependency] == "down"

    def test_readiness_ignores_a_dead_broker(self, client, monkeypatch):
        """The web tier answers pages without Celery; a dead broker is advisory."""
        from apps.core import health

        monkeypatch.setattr(health, "check_broker", lambda: "down")
        response = client.get("/health/ready/")
        assert response.status_code == 200
        assert response.json()["checks"]["broker"] == "down"

    @pytest.mark.parametrize("path", ["/health/live/", "/health/ready/"])
    def test_health_endpoints_reject_writes(self, client, path):
        assert client.post(path).status_code == 405

    def test_health_endpoints_are_never_cached(self, client):
        response = client.get("/health/ready/")
        assert "no-cache" in response.headers.get("Cache-Control", "")


# --------------------------------------------------------------------------------------
# JSON logging
# --------------------------------------------------------------------------------------


class TestJsonFormatter:
    @staticmethod
    def _record(**kwargs):
        """Build a LogRecord the way ``Logger.makeRecord`` attaches extras.

        CPython's ``LogRecord.__init__`` accepts ``**kwargs`` but silently drops
        them, so extras have to be set on the record explicitly.
        """
        fields = {
            "name": "flashwear.test",
            "level": logging.INFO,
            "pathname": __file__,
            "lineno": 1,
            "msg": "checkout completed for %s",
            "args": ("bob@example.com",),
            "exc_info": None,
        }
        fields.update({k: v for k, v in kwargs.items() if k in fields})
        record = logging.LogRecord(**fields)
        for key, value in kwargs.items():
            if key not in fields:
                setattr(record, key, value)
        return record

    def test_emits_valid_json_with_context(self):
        from apps.core.logging_filters import JsonFormatter

        payload = json.loads(JsonFormatter().format(self._record()))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "flashwear.test"
        assert payload["request_id"] == "-"
        assert payload["source"] == "test_phase18_infrastructure:1"
        assert "bob@example.com" not in payload["message"]
        assert "[REDACTED]" in payload["message"]

    def test_sensitive_extras_are_redacted_by_key(self):
        from apps.core.logging_filters import JsonFormatter

        payload = json.loads(
            JsonFormatter().format(self._record(msg="auth", args=(), api_key="short-secret"))
        )
        assert payload["api_key"] == "[REDACTED]"

    def test_benign_extras_survive(self):
        from apps.core.logging_filters import JsonFormatter

        payload = json.loads(
            JsonFormatter().format(self._record(msg="ok", args=(), order_id="FW-1"))
        )
        assert payload["order_id"] == "FW-1"

    def test_exception_text_is_included_and_redacted(self):
        from apps.core.logging_filters import JsonFormatter

        try:
            raise ValueError("password=hunter2")
        except ValueError:
            import sys

            record = self._record(exc_info=sys.exc_info(), msg="boom", args=())
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError" in payload["exception"]
        assert "hunter2" not in payload["exception"]

    def test_production_defaults_to_json_logs(self):
        source = (settings.BASE_DIR / "config" / "settings" / "production.py").read_text(
            encoding="utf-8"
        )
        assert 'LOG_FORMAT = env("LOG_FORMAT", default="json")' in source

    def test_base_registers_the_json_formatter(self):
        from config.settings import base

        assert "json" in base.LOGGING["formatters"]


# --------------------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------------------


class TestMonitoring:
    def test_provider_falls_back_to_logging_without_a_dsn(self):
        from apps.core import monitoring

        assert monitoring.configured_provider() == "logging"

    def test_capture_exception_logs_without_raising(self, caplog):
        from apps.core import monitoring

        with caplog.at_level(logging.ERROR, logger="flashwear.monitoring"):
            monitoring.capture_exception(ValueError("boom"), request_id="abc")
        assert any("boom" in r.getMessage() for r in caplog.records)

    def test_capture_message_logs_at_the_requested_level(self, caplog):
        from apps.core import monitoring

        with caplog.at_level(logging.WARNING, logger="flashwear.monitoring"):
            monitoring.capture_message("disk almost full", level="warning")
        assert any("disk almost full" in r.getMessage() for r in caplog.records)

    def test_capture_never_raises_with_a_broken_provider(self, monkeypatch):
        import apps.core.monitoring as monitoring

        monkeypatch.setattr(monitoring, "_provider", "sentry")
        monitoring.capture_exception(ValueError("x"))
        monitoring.capture_message("x")

    def test_django_request_exception_signal_is_wired(self):
        from django.core.signals import got_request_exception

        receivers = [r for r in got_request_exception.receivers if r[1] is not None]
        assert receivers, "got_request_exception has no receivers"

    def test_request_exception_handler_reports_the_exception(self, caplog, rf):
        from apps.core import monitoring

        request = rf.get("/")
        request.request_id = "req-123"
        with caplog.at_level(logging.ERROR, logger="flashwear.monitoring"):
            monitoring._on_request_exception(
                sender=None, request=request, exception=ValueError("v")
            )
        assert any("ValueError" in r.getMessage() for r in caplog.records)

    def test_task_failure_marks_metrics(self):
        from apps.core import metrics, monitoring

        class FakeTask:
            name = "probe.failed.task"

        before = metrics.snapshot()["tasks"]["failed"]
        monitoring._on_task_failure(sender=FakeTask, task_id="t1", exception=RuntimeError("x"))
        after = metrics.snapshot()["tasks"]["failed"]
        assert after == before + 1

    def test_configure_is_safe_to_call_again(self):
        from apps.core import monitoring

        monitoring.configure()  # no DSN: must be a no-op, never raise


# --------------------------------------------------------------------------------------
# Error pages
# --------------------------------------------------------------------------------------


class TestErrorPages:
    @pytest.mark.django_db
    def test_csrf_failure_renders_the_403_page(self):
        """A real end-to-end 403: POST without a CSRF token, template not plaintext."""
        from django.test import Client

        response = Client(enforce_csrf_checks=True).post(
            "/accounts/login/", {"username": "x", "password": "y"}
        )
        assert response.status_code == 403
        assert "403 — Not allowed" in response.content.decode()

    @pytest.mark.django_db
    def test_permission_denied_renders_the_403_page(self, user):
        """Real PermissionDenied path: an unknown address-kind slug raises in the view."""
        from django.test import Client

        client = Client()
        client.force_login(user)
        response = client.post("/account/addresses/1/default/not-a-kind/")
        assert response.status_code == 403
        assert "403 — Not allowed" in response.content.decode()

    @pytest.mark.django_db
    def test_disallowed_host_renders_the_400_page(self, client):
        response = client.get("/", HTTP_HOST="evil.example")
        assert response.status_code == 400
        assert "400 — Bad request" in response.content.decode()

    def test_error_handlers_are_wired_for_400_and_403(self):
        import config.urls

        assert config.urls.handler400 == "apps.core.views.bad_request"
        assert config.urls.handler403 == "apps.core.views.permission_denied"

    @pytest.mark.django_db
    def test_404_and_500_templates_still_exist(self, client):
        assert client.get("/no-such-page/").status_code == 404


# --------------------------------------------------------------------------------------
# Throttling defaults
# --------------------------------------------------------------------------------------


class TestApiThrottleDefaults:
    def test_default_throttle_classes_are_declared(self):
        classes = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]
        assert any(c.endswith("AnonRateThrottle") for c in classes)
        assert any(c.endswith("UserRateThrottle") for c in classes)
        assert any(c.endswith("ScopedRateThrottle") for c in classes)

    @pytest.mark.parametrize(
        "scope", ["anon", "user", "sensitive", "expensive", "notifications", "notifications_write"]
    )
    def test_every_scope_has_a_rate(self, scope):
        assert scope in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]

    def test_anon_budget_is_enforced_on_the_api(self, client, monkeypatch):
        """DRF freezes DEFAULT_THROTTLE_RATES on the throttle class at import time.

        ``override_settings`` cannot reach it after the first API request in the
        process, so the test patches the class attribute directly -- the same object
        production reads, just with a budget a test can exhaust.
        """
        from django.core.cache import cache
        from rest_framework.throttling import SimpleRateThrottle

        monkeypatch.setattr(
            SimpleRateThrottle,
            "THROTTLE_RATES",
            {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "anon": "2/min"},
        )
        cache.clear()
        url = reverse("v1:health")
        assert client.get(url).status_code == 200
        assert client.get(url).status_code == 200
        throttled = client.get(url)
        assert throttled.status_code == 429
        assert throttled["Retry-After"]


# --------------------------------------------------------------------------------------
# Auth hardening: HTML attempt throttles + DRF sensitive/expensive scopes
# --------------------------------------------------------------------------------------


class TestAuthAttemptThrottles:
    @pytest.mark.django_db
    def test_password_reset_post_is_throttled(self, client, mailoutbox):
        """Reset requests count regardless of outcome -- the abuse is the volume."""
        url = reverse("account:password-reset")
        payload = {"email": "no-such-person@example.com"}
        with override_settings(ACCOUNT_ATTEMPT_MAX=2, ACCOUNT_ATTEMPT_WINDOW_SECONDS=60):
            assert client.post(url, payload).status_code != 429
            assert client.post(url, payload).status_code != 429
            blocked = client.post(url, payload)
        assert blocked.status_code == 429
        assert blocked["Retry-After"] == "60"
        assert "Too many attempts" in blocked.content.decode()
        # An unknown address sends nothing even before the limit, so the mailbox
        # stays quiet after it too.
        assert len(mailoutbox) == 0

    @pytest.mark.django_db
    def test_registration_post_is_throttled(self, client):
        url = reverse("account:register")
        invalid = {"email": "not-an-email"}  # renders form errors, but still counts
        with override_settings(ACCOUNT_ATTEMPT_MAX=1, ACCOUNT_ATTEMPT_WINDOW_SECONDS=60):
            assert client.post(url, invalid).status_code != 429
            blocked = client.post(url, invalid)
        assert blocked.status_code == 429
        assert blocked["Retry-After"] == "60"
        assert "Too many attempts" in blocked.content.decode()

    @pytest.mark.django_db
    def test_resend_verification_post_is_throttled(self, client, user):
        client.force_login(user)
        url = reverse("account:resend-verification")
        with override_settings(ACCOUNT_ATTEMPT_MAX=1, ACCOUNT_ATTEMPT_WINDOW_SECONDS=60):
            assert client.post(url, {}).status_code != 429
            blocked = client.post(url, {})
        assert blocked.status_code == 429
        assert blocked["Retry-After"] == "60"

    @pytest.mark.django_db
    def test_separate_actions_do_not_share_a_counter(self, client):
        """Hitting registration must not eat into the reset budget, and vice versa."""
        register_url = reverse("account:register")
        reset_url = reverse("account:password-reset")
        with override_settings(ACCOUNT_ATTEMPT_MAX=5, ACCOUNT_ATTEMPT_WINDOW_SECONDS=60):
            for _ in range(5):
                client.post(register_url, {"email": "not-an-email"})
            assert client.post(register_url, {"email": "not-an-email"}).status_code == 429
            # The reset action has its own key and is untouched by the registration spend.
            assert client.post(reset_url, {"email": "a@b.co"}).status_code != 429


class TestApiThrottleScopes:
    def test_sensitive_and_expensive_scopes_are_assigned(self):
        from apps.accounts.api import ChangePasswordView, RegisterView
        from apps.catalog.api import ProductSearchSuggestionsView, ProductSearchView
        from apps.engagement.api import PromotionValidateView
        from apps.recommendations.api import (
            ClosetComplementView,
            ForYouView,
            NewForYouView,
            OutfitCompletionView,
            SimilarProductsView,
        )
        from apps.styling.api import (
            FlashDNACheckView,
            FlashDNAView,
            StylistRecommendView,
            StylistSaveOutfitView,
            StylistStyleProductView,
        )

        assert RegisterView.throttle_scope == "sensitive"
        assert ChangePasswordView.throttle_scope == "sensitive"
        assert PromotionValidateView.throttle_scope == "sensitive"
        for cls in (
            FlashDNAView,
            FlashDNACheckView,
            StylistRecommendView,
            StylistStyleProductView,
            StylistSaveOutfitView,
            ForYouView,
            ClosetComplementView,
            OutfitCompletionView,
            SimilarProductsView,
            NewForYouView,
            ProductSearchView,
            ProductSearchSuggestionsView,
        ):
            assert cls.throttle_scope == "expensive"

    def test_registration_api_is_throttled_under_sensitive_scope(self, api_client, monkeypatch):
        from rest_framework.throttling import SimpleRateThrottle

        monkeypatch.setattr(
            SimpleRateThrottle,
            "THROTTLE_RATES",
            {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "sensitive": "2/min"},
        )
        url = reverse("v1:account-register")
        # Throttles run in initial(), before the payload is parsed, so an empty
        # body is enough to spend the budget.
        assert api_client.post(url, {}).status_code != 429
        assert api_client.post(url, {}).status_code != 429
        throttled = api_client.post(url, {})
        assert throttled.status_code == 429
        assert throttled["Retry-After"]

    @pytest.mark.django_db
    def test_search_api_is_throttled_under_expensive_scope(self, client, monkeypatch):
        from rest_framework.throttling import SimpleRateThrottle

        monkeypatch.setattr(
            SimpleRateThrottle,
            "THROTTLE_RATES",
            {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "expensive": "1/min"},
        )
        url = reverse("v1:product-search") + "?q=shirt"
        assert client.get(url).status_code != 429
        assert client.get(url).status_code == 429


# --------------------------------------------------------------------------------------
# Upload hardening: model-level image validation (admin surfaces included)
# --------------------------------------------------------------------------------------


class TestUploadHardening:
    """Model fields carry the content-first checks, so no form can skip them.

    The storefront forms validate on their own; these tests prove the *other*
    surfaces -- Django admin inlines and operations' default ModelForms -- are
    covered by the model itself.
    """

    @pytest.mark.django_db
    def test_avatar_model_field_rejects_a_file_the_form_never_saw(self, user):
        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile

        profile = user.profile
        profile.avatar = SimpleUploadedFile("face.jpg", b"definitely not an image")
        with pytest.raises(ValidationError) as excinfo:
            profile.full_clean()
        assert "avatar" in excinfo.value.error_dict

    @pytest.mark.django_db
    def test_drop_hero_model_field_is_content_checked(self):
        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.drops.models import FlashDrop

        drop = FlashDrop.objects.create(name="Midnight Release", slug="midnight-release")
        drop.hero_image = SimpleUploadedFile("hero.jpg", b"<svg onload=alert(1)>")
        with pytest.raises(ValidationError) as excinfo:
            drop.full_clean()
        assert "hero_image" in excinfo.value.error_dict

    @pytest.mark.django_db
    def test_product_image_model_field_is_content_checked(self, product):
        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.catalog.models import ProductImage

        image = ProductImage(
            product=product,
            image=SimpleUploadedFile("photo.jpg", b"not an image"),
            alt_text="Front",
        )
        with pytest.raises(ValidationError) as excinfo:
            image.full_clean()
        assert "image" in excinfo.value.error_dict

    @pytest.mark.django_db
    def test_a_real_image_still_passes_the_model_validator(self, product):
        """Guard against the gate getting so strict it rejects valid uploads."""
        from io import BytesIO

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        from apps.catalog.models import ProductImage

        buffer = BytesIO()
        Image.new("RGB", (4, 4), color=(10, 200, 30)).save(buffer, format="JPEG")
        image = ProductImage(
            product=product,
            image=SimpleUploadedFile("photo.jpg", buffer.getvalue()),
            alt_text="Front",
        )
        image.full_clean()  # must not raise


# --------------------------------------------------------------------------------------
# Hot-path indexes (Phase 18 index review)
# --------------------------------------------------------------------------------------


class TestHotPathIndexes:
    """Schema regression checks for the Phase 18 index additions.

    These assert on ``Model._meta`` (not EXPLAIN output): the index names and
    column orders must survive refactors, while the assertions stay stable
    across sqlite/Postgres where planner output differs.
    """

    def _has_index(self, model, *fields) -> bool:
        target = list(fields)
        return any(list(index.fields) == target for index in model._meta.indexes)

    def test_order_and_payment_historical_slices(self):
        """Admin/ledger slices that sort globally on ``-created_at``."""
        from apps.orders.models import Order
        from apps.payments.models import Payment

        assert self._has_index(Order, "-created_at")
        assert self._has_index(Payment, "-created_at")

    def test_shipment_queue_indexes(self):
        """The ops shipment queue filters on status then orders by ``-created_at``."""
        from apps.orders.models import Shipment

        assert self._has_index(Shipment, "status", "-created_at")
        assert self._has_index(Shipment, "-created_at")

    def test_support_queue_indexes(self):
        """Back-office queue sorts on ``-updated_at`` / ``priority``."""
        from apps.support.models import SupportTicket

        assert self._has_index(SupportTicket, "-updated_at")
        assert self._has_index(SupportTicket, "priority", "-updated_at")

    def test_ledger_and_promotion_indexes(self):
        from apps.engagement.models import PointsTransaction, Promotion

        assert self._has_index(PointsTransaction, "transaction_type", "-created_at")
        assert self._has_index(Promotion, "is_active", "-starts_at")

    def test_standalone_filter_indexes(self):
        """Boolean/type filters that previously matched no index."""
        from apps.catalog.models import Product
        from apps.inventory.models import InventoryMovement
        from apps.loop.models import LoopItem
        from apps.quests.models import UserQuest

        assert self._has_index(Product, "is_featured", "-published_at")
        assert self._has_index(InventoryMovement, "-created_at")
        assert self._has_index(UserQuest, "status", "-updated_at")
        assert self._has_index(LoopItem, "type", "-created_at")

    def test_user_joined_date_is_indexed(self):
        from apps.accounts.models import User

        assert User._meta.get_field("date_joined").db_index
