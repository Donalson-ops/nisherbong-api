from fastapi import Depends, FastAPI

from backend.admin import create_admin_router
from backend.auth import create_auth_router
from backend.billing import create_billing_router
from backend.settings import Settings
from backend.supabase_client import (
    admin_headers,
    admin_user,
    authenticated_user,
    get_settings,
    request_supabase,
)


def create_app() -> FastAPI:
    app = FastAPI(title="Nisherbong API", version="0.1.0")
    app.include_router(
        create_auth_router(get_settings, authenticated_user)
    )
    app.include_router(
        create_admin_router(
            get_settings,
            admin_user,
            request_supabase,
            admin_headers,
        )
    )
    app.include_router(
        create_billing_router(
            get_settings,
            authenticated_user,
            request_supabase,
            admin_headers,
        )
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/me")
    async def get_current_user(
        user: dict = Depends(authenticated_user),
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str | None]:
        response = await request_supabase(
            "GET",
            f"{settings.supabase_url}/rest/v1/profiles",
            admin_headers(settings),
            params={"select": "role", "id": f"eq.{user['id']}", "limit": "1"},
        )
        profiles = response.json()
        role = profiles[0].get("role") if profiles else "user"
        return {
            "id": str(user["id"]),
            "email": user.get("email"),
            "role": role,
        }

    return app


app = create_app()
