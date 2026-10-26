from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field

from backend.settings import Settings


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class EmailRequest(BaseModel):
    email: EmailStr


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=4096)


class PasswordUpdate(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class GoogleExchange(BaseModel):
    auth_code: str = Field(min_length=8, max_length=2048)
    code_verifier: str = Field(min_length=43, max_length=128)


def create_auth_router(
    get_settings: Callable[[], Settings],
    get_authenticated_user: Callable[..., Awaitable[dict]],
) -> APIRouter:
    router = APIRouter(prefix="/v1/auth", tags=["authentication"])

    async def request_auth(
        method: str,
        endpoint: str,
        settings: Settings,
        *,
        payload: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> Any:
        headers = {"apikey": settings.supabase_anon_key}
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token}"
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                response = await client.request(
                    method,
                    f"{settings.supabase_url}/auth/v1/{endpoint}",
                    headers=headers,
                    json=payload,
                )
            except httpx.HTTPError as erro:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Falha ao conectar ao serviço de autenticação: {erro}",
                ) from erro
        if response.is_error:
            try:
                body = response.json()
            except ValueError:
                body = {}
            detail = (
                body.get("msg")
                or body.get("message")
                or "A operação de autenticação foi recusada."
                if isinstance(body, dict)
                else "A operação de autenticação foi recusada."
            )
            code = (
                response.status_code
                if 400 <= response.status_code < 500
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=code, detail=detail)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as erro:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O serviço de autenticação retornou uma resposta inválida.",
            ) from erro

    @router.post("/signup", status_code=status.HTTP_201_CREATED)
    async def signup(
        credentials: Credentials,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        result = await request_auth(
            "POST",
            "signup",
            settings,
            payload={
                "email": str(credentials.email),
                "password": credentials.password,
            },
        )
        if not isinstance(result, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O serviço de autenticação retornou uma resposta inválida.",
            )
        user = result.get("user") or {}
        return {
            "status": (
                "confirmation_required"
                if user and not user.get("email_confirmed_at")
                else "created"
            ),
            "message": (
                "Se o cadastro for válido, verifique seu email para confirmar "
                "a conta antes de entrar."
            ),
        }

    @router.post("/login")
    async def login(
        credentials: Credentials,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        result = await request_auth(
            "POST",
            "token?grant_type=password",
            settings,
            payload={
                "email": str(credentials.email),
                "password": credentials.password,
            },
        )
        if not isinstance(result, dict) or not result.get("access_token"):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O serviço de autenticação não retornou uma sessão válida.",
            )
        return result

    @router.get("/google/url")
    async def google_url(
        redirect_to: str = Query(max_length=200),
        code_challenge: str = Query(min_length=43, max_length=128),
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        destino = urlparse(redirect_to)
        if destino.scheme != "http" or destino.hostname not in (
            "127.0.0.1",
            "localhost",
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="O retorno do login deve ser um endereço local.",
            )
        consulta = urlencode(
            {
                "provider": "google",
                "redirect_to": redirect_to,
                "code_challenge": code_challenge,
                "code_challenge_method": "s256",
            }
        )
        return {"url": f"{settings.supabase_url}/auth/v1/authorize?{consulta}"}

    @router.post("/google/exchange")
    async def google_exchange(
        request: GoogleExchange,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        result = await request_auth(
            "POST",
            "token?grant_type=pkce",
            settings,
            payload={
                "auth_code": request.auth_code,
                "code_verifier": request.code_verifier,
            },
        )
        if not isinstance(result, dict) or not result.get("access_token"):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O serviço de autenticação não retornou uma sessão válida.",
            )
        return result

    @router.post("/refresh")
    async def refresh_session(
        request: RefreshRequest,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        result = await request_auth(
            "POST",
            "token?grant_type=refresh_token",
            settings,
            payload={"refresh_token": request.refresh_token},
        )
        if not isinstance(result, dict) or not result.get("access_token"):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O serviço de autenticação não retornou uma sessão válida.",
            )
        return result

    @router.post("/password/recovery")
    async def recover_password(
        request: EmailRequest,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        await request_auth(
            "POST",
            "recover",
            settings,
            payload={"email": str(request.email)},
        )
        return {
            "status": "accepted",
            "message": (
                "Se houver uma conta com esse email, você receberá instruções "
                "para redefinir a senha."
            ),
        }

    @router.put("/password")
    async def update_password(
        request: PasswordUpdate,
        authorization: str | None = Header(default=None),
        usuario: dict = Depends(get_authenticated_user),
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        _ = usuario
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Informe um token de sessão válido.",
            )
        await request_auth(
            "PUT",
            "user",
            settings,
            payload={"password": request.password},
            access_token=authorization.removeprefix("Bearer "),
        )
        return {"status": "updated"}

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        authorization: str | None = Header(default=None),
        usuario: dict = Depends(get_authenticated_user),
        settings: Settings = Depends(get_settings),
    ) -> None:
        _ = usuario
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Informe um token de sessão válido.",
            )
        await request_auth(
            "POST",
            "logout",
            settings,
            access_token=authorization.removeprefix("Bearer "),
        )

    return router
