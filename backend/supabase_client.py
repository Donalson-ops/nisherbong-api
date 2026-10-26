from functools import lru_cache

import httpx
from fastapi import Depends, Header, HTTPException, status

from backend.settings import Settings


@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()


def admin_headers(settings: Settings) -> dict[str, str]:
    return {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
    }


async def request_supabase(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    params: dict[str, str] | None = None,
    json_body: dict | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as erro:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha ao conectar ao Supabase: {erro}",
            ) from erro
    if response.is_error:
        try:
            body = response.json()
        except ValueError:
            body = {}
        detail = (
            body.get("message")
            or body.get("msg")
            or "O Supabase recusou a operação solicitada."
            if isinstance(body, dict)
            else "O Supabase recusou a operação solicitada."
        )
        code = (
            response.status_code
            if 400 <= response.status_code < 500
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=code, detail=detail)
    return response


async def authenticated_user(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> dict:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Informe um token de sessão válido.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    response = await request_supabase(
        "GET",
        f"{settings.supabase_url}/auth/v1/user",
        {
            "apikey": settings.supabase_anon_key,
            "Authorization": authorization,
        },
    )
    user = response.json()
    if not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A sessão retornada pelo Supabase é inválida.",
        )
    return user


async def admin_user(
    user: dict = Depends(authenticated_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    response = await request_supabase(
        "GET",
        f"{settings.supabase_url}/rest/v1/profiles",
        admin_headers(settings),
        params={
            "select": "role",
            "id": f"eq.{user['id']}",
            "limit": "1",
        },
    )
    profiles = response.json()
    if (
        not isinstance(profiles, list)
        or not profiles
        or profiles[0].get("role") != "admin"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta operação exige uma conta administradora.",
        )
    return user
