import hashlib
import hmac
import json
import time
import unittest
from dataclasses import replace
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.main import app
from backend.settings import Settings
from backend.supabase_client import get_settings


USER_ID = "11111111-1111-1111-1111-111111111111"
TEST_SETTINGS = Settings(
    supabase_url="https://example.supabase.co",
    supabase_anon_key="public-test-key",
    supabase_service_role_key="server-test-key",
    resend_api_key=None,
    resend_from_email=None,
    stripe_webhook_secret="webhook-test-secret",
)


class BillingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def _mock_http(self, callback):
        async_client = httpx.AsyncClient

        def create_async_client(*args, **kwargs) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(callback)
            return async_client(*args, **kwargs)

        return patch("httpx.AsyncClient", new=create_async_client)

    def test_checkout_requires_server_stripe_configuration(self) -> None:
        def mock_request(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/auth/v1/user")
            return httpx.Response(200, json={"id": USER_ID})

        with self._mock_http(mock_request):
            response = self.client.post(
                "/v1/account/checkout",
                headers={"Authorization": "Bearer user-token"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("Stripe", response.json()["detail"])

    def test_checkout_creates_a_monthly_stripe_session(self) -> None:
        settings = replace(
            TEST_SETTINGS,
            stripe_secret_key="sk_test_for_tests",
            stripe_price_id="price_monthly_test",
            stripe_success_url="https://example.com/success",
            stripe_cancel_url="https://example.com/cancel",
        )
        app.dependency_overrides[get_settings] = lambda: settings

        def mock_request(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={"id": USER_ID})
            if request.url.path == "/rest/v1/subscriptions":
                if request.method == "GET":
                    return httpx.Response(200, json=[])
                payload = json.loads(request.content)
                self.assertEqual(payload["plan"], "Plano Pro")
                self.assertEqual(payload["status"], "pending")
                return httpx.Response(201, json=[])
            self.assertEqual(request.url.path, "/v1/checkout/sessions")
            form = parse_qs(request.content.decode())
            self.assertEqual(form["mode"], ["subscription"])
            self.assertEqual(form["line_items[0][price]"], ["price_monthly_test"])
            self.assertEqual(form["line_items[0][quantity]"], ["1"])
            self.assertEqual(form["subscription_data[metadata][user_id]"], [USER_ID])
            return httpx.Response(
                200,
                json={
                    "id": "cs_test_session",
                    "url": "https://checkout.stripe.test/session",
                },
            )

        with self._mock_http(mock_request):
            response = self.client.post(
                "/v1/account/checkout",
                headers={"Authorization": "Bearer user-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["url"], "https://checkout.stripe.test/session"
        )

    def test_account_deletion_is_blocked_by_active_subscription(self) -> None:
        def mock_request(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={"id": USER_ID})
            self.assertEqual(request.url.path, "/rest/v1/subscriptions")
            self.assertEqual(
                request.url.params["status"],
                "in.(pending,trialing,active,past_due)",
            )
            return httpx.Response(200, json=[{"id": "subscription-id"}])

        with self._mock_http(mock_request):
            response = self.client.delete(
                "/v1/account",
                headers={"Authorization": "Bearer user-token"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("Cancele a assinatura", response.json()["detail"])

    def test_webhook_rejects_invalid_signature(self) -> None:
        response = self.client.post(
            "/v1/account/stripe/webhook",
            content=b'{"type":"customer.subscription.updated"}',
            headers={"Stripe-Signature": "t=1,v1=invalid"},
        )

        self.assertEqual(response.status_code, 400)

    def test_webhook_accepts_a_valid_signature(self) -> None:
        body = json.dumps({"type": "unhandled.event", "data": {"object": {}}})
        timestamp = int(time.time())
        signed_payload = f"{timestamp}.{body}".encode()
        signature = hmac.new(
            TEST_SETTINGS.stripe_webhook_secret.encode(),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        response = self.client.post(
            "/v1/account/stripe/webhook",
            content=body,
            headers={"Stripe-Signature": f"t={timestamp},v1={signature}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"received": True})

    def test_subscription_webhook_persists_status_and_period_end(self) -> None:
        calls: list[httpx.Request] = []
        event = {
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_test_monthly",
                    "customer": "cus_test",
                    "status": "active",
                    "current_period_end": 1793000000,
                    "cancel_at_period_end": False,
                    "metadata": {"user_id": USER_ID, "plan": "Plano Pro"},
                }
            },
        }
        body = json.dumps(event)
        timestamp = int(time.time())
        signed_payload = f"{timestamp}.{body}".encode()
        signature = hmac.new(
            TEST_SETTINGS.stripe_webhook_secret.encode(),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        def mock_request(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.method == "PATCH":
                self.assertEqual(
                    request.url.params["provider_subscription_id"], "is.null"
                )
                return httpx.Response(200, json=[])
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                request.url.params["on_conflict"],
                "provider,provider_subscription_id",
            )
            payload = json.loads(request.content)
            self.assertEqual(payload["user_id"], USER_ID)
            self.assertEqual(payload["provider_subscription_id"], "sub_test_monthly")
            self.assertEqual(payload["status"], "active")
            self.assertEqual(payload["plan"], "Plano Pro")
            return httpx.Response(201, json=[])

        with self._mock_http(mock_request):
            response = self.client.post(
                "/v1/account/stripe/webhook",
                content=body,
                headers={"Stripe-Signature": f"t={timestamp},v1={signature}"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"received": True})
        self.assertEqual(len(calls), 2)
