import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.settings import Settings
from backend.main import app
from backend.supabase_client import get_settings


ADMIN_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"

TEST_SETTINGS = Settings(
    supabase_url="https://example.supabase.co",
    supabase_anon_key="public-test-key",
    supabase_service_role_key="server-test-key",
    resend_api_key=None,
    resend_from_email=None,
)


class BackendApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def test_health_endpoint_does_not_require_supabase(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_signup_uses_supabase_email_confirmation(self) -> None:
        def mock_request(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.url.path,
                "/auth/v1/signup",
            )
            self.assertEqual(
                request.headers["apikey"],
                TEST_SETTINGS.supabase_anon_key,
            )
            return httpx.Response(
                200,
                json={
                    "user": {
                        "id": "user-id",
                        "email": "person@example.com",
                        "email_confirmed_at": None,
                    },
                    "session": None,
                },
            )

        async_client = httpx.AsyncClient

        def create_async_client(*args, **kwargs) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(mock_request)
            return async_client(*args, **kwargs)

        with patch(
            "httpx.AsyncClient",
            new=create_async_client,
        ):
            response = self.client.post(
                "/v1/auth/signup",
                json={
                    "email": "person@example.com",
                    "password": "a-strong-test-password",
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json()["status"],
            "confirmation_required",
        )
        self.assertNotIn("access_token", response.json())

    def test_login_returns_session_from_supabase(self) -> None:
        expected_session = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "token_type": "bearer",
            "expires_in": 3600,
        }

        def mock_request(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/auth/v1/token")
            self.assertEqual(
                request.url.params["grant_type"],
                "password",
            )
            self.assertEqual(request.method, "POST"            )
            return httpx.Response(200, json=expected_session)

        async_client = httpx.AsyncClient

        def create_async_client(*args, **kwargs) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(mock_request)
            return async_client(*args, **kwargs)

        with patch(
            "httpx.AsyncClient",
            new=create_async_client,
        ):
            response = self.client.post(
                "/v1/auth/login",
                json={
                    "email": "person@example.com",
                    "password": "a-strong-test-password",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected_session)

    def test_login_rejects_invalid_email_before_network_call(self) -> None:
        response = self.client.post(
            "/v1/auth/login",
            json={"email": "not-an-email", "password": "password123"},
        )

        self.assertEqual(response.status_code, 422)

    def test_admin_routes_reject_regular_accounts(self) -> None:
        def mock_request(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={"id": "user-id"})
            self.assertEqual(request.url.path, "/rest/v1/profiles")
            return httpx.Response(200, json=[{"role": "user"}])

        async_client = httpx.AsyncClient

        def create_async_client(*args, **kwargs) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(mock_request)
            return async_client(*args, **kwargs)

        with patch("httpx.AsyncClient", new=create_async_client):
            response = self.client.get(
                "/v1/admin/users",
                headers={"Authorization": "Bearer user-access-token"},
            )

        self.assertEqual(response.status_code, 403)

    def test_admin_user_listing_is_registered_and_uses_admin_access(self) -> None:
        def mock_request(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={"id": "admin-id"})
            if request.url.path == "/rest/v1/profiles":
                return httpx.Response(200, json=[{"role": "admin"}])
            self.assertEqual(request.url.path, "/auth/v1/admin/users")
            self.assertEqual(request.url.params["page"], "2")
            self.assertEqual(request.url.params["per_page"], "10")
            return httpx.Response(
                200,
                json={
                    "users": [
                        {
                            "id": "user-id",
                            "email": "person@example.com",
                            "email_confirmed_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                },
            )

        async_client = httpx.AsyncClient

        def create_async_client(*args, **kwargs) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(mock_request)
            return async_client(*args, **kwargs)

        with patch("httpx.AsyncClient", new=create_async_client):
            response = self.client.get(
                "/v1/admin/users?page=2&per_page=10",
                headers={"Authorization": "Bearer test-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["page"], 2)
        self.assertEqual(response.json()["per_page"], 10)
        self.assertEqual(response.json()["users"][0]["id"], "user-id")

    def test_admin_can_delete_other_users_but_not_self(self) -> None:
        calls: list[tuple[str, str]] = []

        def mock_request(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={"id": ADMIN_ID})
            if request.url.path == "/rest/v1/profiles":
                return httpx.Response(200, json=[{"role": "admin"}])
            calls.append((request.method, request.url.path))
            return httpx.Response(200, json={})

        async_client = httpx.AsyncClient

        def create_async_client(*args, **kwargs) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(mock_request)
            return async_client(*args, **kwargs)

        headers = {"Authorization": "Bearer test-token"}
        with patch("httpx.AsyncClient", new=create_async_client):
            ok = self.client.delete(f"/v1/admin/users/{OTHER_ID}", headers=headers)
            self_delete = self.client.delete(f"/v1/admin/users/{ADMIN_ID}", headers=headers)

        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self_delete.status_code, 400)
        self.assertEqual(calls, [("DELETE", f"/auth/v1/admin/users/{OTHER_ID}")])

    def test_current_user_includes_role(self) -> None:
        def mock_request(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={"id": ADMIN_ID, "email": "a@b.com"})
            return httpx.Response(200, json=[{"role": "admin"}])

        async_client = httpx.AsyncClient

        def create_async_client(*args, **kwargs) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(mock_request)
            return async_client(*args, **kwargs)

        with patch("httpx.AsyncClient", new=create_async_client):
            response = self.client.get("/v1/me", headers={"Authorization": "Bearer t"})

        self.assertEqual(response.json()["role"], "admin")

    def test_google_url_only_accepts_local_redirects(self) -> None:
        desafio = "a" * 43
        ok = self.client.get(
            "/v1/auth/google/url",
            params={"redirect_to": "http://127.0.0.1:54321/callback", "code_challenge": desafio},
        )
        ruim = self.client.get(
            "/v1/auth/google/url",
            params={"redirect_to": "https://evil.example/x", "code_challenge": desafio},
        )

        self.assertEqual(ok.status_code, 200)
        self.assertIn("provider=google", ok.json()["url"])
        self.assertIn("code_challenge_method=s256", ok.json()["url"])
        self.assertEqual(ruim.status_code, 400)

    def test_google_exchange_uses_pkce_grant(self) -> None:
        def mock_request(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/auth/v1/token")
            self.assertEqual(request.url.params["grant_type"], "pkce")
            return httpx.Response(200, json={"access_token": "t", "refresh_token": "r"})

        async_client = httpx.AsyncClient

        def create_async_client(*args, **kwargs) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(mock_request)
            return async_client(*args, **kwargs)

        with patch("httpx.AsyncClient", new=create_async_client):
            response = self.client.post(
                "/v1/auth/google/exchange",
                json={"auth_code": "codigo-de-teste", "code_verifier": "v" * 50},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["access_token"], "t")

    def test_auth_provider_errors_keep_client_status(self) -> None:
        def mock_request(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"msg": "Invalid login credentials"},
            )

        async_client = httpx.AsyncClient

        def create_async_client(*args, **kwargs) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(mock_request)
            return async_client(*args, **kwargs)

        with patch("httpx.AsyncClient", new=create_async_client):
            response = self.client.post(
                "/v1/auth/login",
                json={
                    "email": "person@example.com",
                    "password": "a-strong-test-password",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "Invalid login credentials",
        )

    def test_password_recovery_does_not_disclose_account_existence(self) -> None:
        def mock_request(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/auth/v1/recover")
            return httpx.Response(200, json={})

        async_client = httpx.AsyncClient

        def create_async_client(*args, **kwargs) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(mock_request)
            return async_client(*args, **kwargs)

        with patch("httpx.AsyncClient", new=create_async_client):
            response = self.client.post(
                "/v1/auth/password/recovery",
                json={"email": "person@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Se houver uma conta",
            response.json()["message"],
        )

    def test_current_user_requires_a_bearer_token(self) -> None:
        response = self.client.get("/v1/me")

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
