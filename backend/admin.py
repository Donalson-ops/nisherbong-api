from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from backend.settings import Settings


class RoleUpdate(BaseModel):
    role: Literal["user", "admin"]


class AppSettingUpdate(BaseModel):
    value: dict | list | str | int | float | bool | None


class NotificationRequest(BaseModel):
    user_id: UUID
    subject: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=5000)

    @field_validator("subject", "message")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("O campo não pode estar vazio.")
        return value


class SubscriptionCreate(BaseModel):
    user_id: UUID
    plan: str = Field(min_length=1, max_length=100)
    status: Literal[
        "pending", "trialing", "active", "past_due", "cancelled", "expired"
    ] = "active"
    current_period_end: datetime | None = None


class SubscriptionUpdate(BaseModel):
    plan: str | None = Field(default=None, min_length=1, max_length=100)
    status: (
        Literal[
            "pending", "trialing", "active", "past_due", "cancelled", "expired"
        ]
        | None
    ) = None
    current_period_end: datetime | None = None


def create_admin_router(
    get_settings: Callable[[], Settings],
    get_admin_user: Callable[..., Awaitable[dict]],
    request_supabase: Callable[..., Awaitable[httpx.Response]],
    admin_headers: Callable[[Settings], dict[str, str]],
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin", tags=["administration"])

    @router.get("/users")
    async def list_users(
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=50, ge=1, le=100),
        _: dict = Depends(get_admin_user),
        settings: Settings = Depends(get_settings),
    ) -> dict:
        resposta = await request_supabase(
            "GET",
            f"{settings.supabase_url}/auth/v1/admin/users",
            admin_headers(settings),
            params={"page": str(page), "per_page": str(per_page)},
        )
        resultado = resposta.json()
        usuarios = resultado.get("users", []) if isinstance(resultado, dict) else []
        return {
            "users": [
                {
                    "id": usuario.get("id"),
                    "email": usuario.get("email"),
                    "email_confirmed_at": usuario.get("email_confirmed_at"),
                    "created_at": usuario.get("created_at"),
                    "last_sign_in_at": usuario.get("last_sign_in_at"),
                }
                for usuario in usuarios
            ],
            "page": page,
            "per_page": per_page,
        }

    @router.patch("/users/{user_id}/role")
    async def change_user_role(
        user_id: UUID,
        update: RoleUpdate,
        usuario: dict = Depends(get_admin_user),
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        if str(user_id) == usuario["id"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Use outra conta administradora para alterar sua "
                    "própria função."
                ),
            )
        await request_supabase(
            "PATCH",
            f"{settings.supabase_url}/rest/v1/profiles",
            {
                **admin_headers(settings),
                "Prefer": "return=minimal",
            },
            params={"id": f"eq.{user_id}"},
            json_body={"role": update.role},
        )
        return {"user_id": str(user_id), "role": update.role}

    @router.delete("/users/{user_id}")
    async def delete_user(
        user_id: UUID,
        admin: dict = Depends(get_admin_user),
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        if str(user_id) == admin["id"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Você não pode remover a própria conta administradora.",
            )
        await request_supabase(
            "DELETE",
            f"{settings.supabase_url}/auth/v1/admin/users/{user_id}",
            admin_headers(settings),
        )
        return {"user_id": str(user_id), "status": "deleted"}

    @router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
    async def create_subscription(
        subscription: SubscriptionCreate,
        _: dict = Depends(get_admin_user),
        settings: Settings = Depends(get_settings),
    ) -> dict:
        body = {
            "user_id": str(subscription.user_id),
            "provider": "manual",
            "plan": subscription.plan,
            "status": subscription.status,
        }
        if subscription.current_period_end:
            body["current_period_end"] = (
                subscription.current_period_end.isoformat()
            )
        resposta = await request_supabase(
            "POST",
            f"{settings.supabase_url}/rest/v1/subscriptions",
            {**admin_headers(settings), "Prefer": "return=representation"},
            json_body=body,
        )
        rows = resposta.json()
        if not isinstance(rows, list) or not rows:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Supabase não confirmou a criação da assinatura.",
            )
        return rows[0]

    @router.patch("/subscriptions/{subscription_id}")
    async def update_subscription(
        subscription_id: UUID,
        update: SubscriptionUpdate,
        _: dict = Depends(get_admin_user),
        settings: Settings = Depends(get_settings),
    ) -> dict:
        body = update.model_dump(exclude_none=True, mode="json")
        if not body:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Informe ao menos um campo para atualizar.",
            )
        resposta = await request_supabase(
            "PATCH",
            f"{settings.supabase_url}/rest/v1/subscriptions",
            {**admin_headers(settings), "Prefer": "return=representation"},
            params={"id": f"eq.{subscription_id}"},
            json_body=body,
        )
        rows = resposta.json()
        if not isinstance(rows, list) or not rows:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assinatura não encontrada.",
            )
        return rows[0]

    @router.get("/subscriptions")
    async def list_subscriptions(
        limit: int = Query(default=50, ge=1, le=100),
        _: dict = Depends(get_admin_user),
        settings: Settings = Depends(get_settings),
    ) -> list[dict]:
        resposta = await request_supabase(
            "GET",
            f"{settings.supabase_url}/rest/v1/subscriptions",
            admin_headers(settings),
            params={
                "select": (
                    "id,user_id,provider,provider_subscription_id,plan,"
                    "status,current_period_end,created_at"
                ),
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )
        resultado = resposta.json()
        if not isinstance(resultado, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Supabase retornou assinaturas em formato inválido.",
            )
        return resultado

    @router.get("/settings")
    async def list_app_settings(
        _: dict = Depends(get_admin_user),
        settings: Settings = Depends(get_settings),
    ) -> list[dict]:
        resposta = await request_supabase(
            "GET",
            f"{settings.supabase_url}/rest/v1/app_settings",
            admin_headers(settings),
            params={
                "select": "key,value,updated_at",
                "order": "key.asc",
            },
        )
        resultado = resposta.json()
        if not isinstance(resultado, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Supabase retornou configurações em formato inválido.",
            )
        return resultado

    @router.get("/notifications")
    async def list_notifications(
        limit: int = Query(default=50, ge=1, le=100),
        _: dict = Depends(get_admin_user),
        settings: Settings = Depends(get_settings),
    ) -> list[dict]:
        resposta = await request_supabase(
            "GET",
            f"{settings.supabase_url}/rest/v1/notifications",
            admin_headers(settings),
            params={
                "select": (
                    "id,user_id,created_by,provider,subject,status,"
                    "provider_message_id,error_message,created_at,sent_at"
                ),
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )
        resultado = resposta.json()
        if not isinstance(resultado, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Supabase retornou notificações em formato inválido.",
            )
        return resultado

    @router.put("/settings/{key}")
    async def update_app_setting(
        key: str,
        update: AppSettingUpdate,
        admin: dict = Depends(get_admin_user),
        settings: Settings = Depends(get_settings),
    ) -> dict:
        if not key or len(key) > 100:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A chave deve ter de 1 a 100 caracteres.",
            )
        resposta = await request_supabase(
            "POST",
            f"{settings.supabase_url}/rest/v1/app_settings",
            {
                **admin_headers(settings),
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
            params={"on_conflict": "key"},
            json_body={
                "key": key,
                "value": update.value,
                "updated_by": admin["id"],
            },
        )
        resultado = resposta.json()
        if not isinstance(resultado, list) or not resultado:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Supabase não confirmou a atualização da configuração.",
            )
        return resultado[0]

    @router.post("/notifications", status_code=status.HTTP_202_ACCEPTED)
    async def send_notification(
        notification: NotificationRequest,
        admin: dict = Depends(get_admin_user),
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        if not settings.resend_api_key or not settings.resend_from_email:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Configure RESEND_API_KEY e RESEND_FROM_EMAIL "
                    "no backend para habilitar notificações."
                ),
            )

        usuario_response = await request_supabase(
            "GET",
            f"{settings.supabase_url}/auth/v1/admin/users/"
            f"{notification.user_id}",
            admin_headers(settings),
        )
        usuario = usuario_response.json()
        email = usuario.get("email") if isinstance(usuario, dict) else None
        if not email:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Usuário não encontrado ou sem email.",
            )

        notification_response = await request_supabase(
            "POST",
            f"{settings.supabase_url}/rest/v1/notifications",
            {
                **admin_headers(settings),
                "Prefer": "return=representation",
            },
            json_body={
                "user_id": str(notification.user_id),
                "created_by": admin["id"],
                "subject": notification.subject,
                "status": "pending",
            },
        )
        notification_rows = notification_response.json()
        if not isinstance(notification_rows, list) or not notification_rows:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Supabase não confirmou o registro da notificação.",
            )
        notification_id = notification_rows[0].get("id")
        if not notification_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Supabase retornou uma notificação sem identificador.",
            )

        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resposta = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {settings.resend_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": settings.resend_from_email,
                        "to": [email],
                        "subject": notification.subject,
                        "text": notification.message,
                    },
                )
            except httpx.HTTPError as erro:
                await request_supabase(
                    "PATCH",
                    f"{settings.supabase_url}/rest/v1/notifications",
                    {
                        **admin_headers(settings),
                        "Prefer": "return=minimal",
                    },
                    params={"id": f"eq.{notification_id}"},
                    json_body={"status": "failed", "error_message": str(erro)},
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Falha ao conectar ao serviço de email: {erro}",
                ) from erro
        if resposta.is_error:
            await request_supabase(
                "PATCH",
                f"{settings.supabase_url}/rest/v1/notifications",
                {
                    **admin_headers(settings),
                    "Prefer": "return=minimal",
                },
                params={"id": f"eq.{notification_id}"},
                json_body={
                    "status": "failed",
                    "error_message": "O serviço de email recusou o envio.",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O serviço de email recusou o envio da notificação.",
            )
        try:
            provider_message_id = resposta.json().get("id")
        except ValueError:
            provider_message_id = None
        await request_supabase(
            "PATCH",
            f"{settings.supabase_url}/rest/v1/notifications",
            {
                **admin_headers(settings),
                "Prefer": "return=minimal",
            },
            params={"id": f"eq.{notification_id}"},
            json_body={
                "status": "sent",
                "provider_message_id": provider_message_id,
                "sent_at": datetime.now().astimezone().isoformat(),
            },
        )
        return {
            "status": "accepted",
            "user_id": str(notification.user_id),
            "notification_id": str(notification_id),
        }

    return router
