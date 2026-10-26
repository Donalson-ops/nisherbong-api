import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from backend.settings import Settings


class CancelSubscriptionRequest(BaseModel):
    at_period_end: bool


def _stripe_headers(settings: Settings) -> dict[str, str]:
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A integração Stripe ainda não está configurada.",
        )
    return {"Authorization": f"Bearer {settings.stripe_secret_key}"}


async def _stripe_request(
    method: str,
    path: str,
    settings: Settings,
    *,
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = _stripe_headers(settings)
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.request(
                method,
                f"https://api.stripe.com/v1/{path}",
                headers=headers,
                data=data,
            )
        except httpx.HTTPError as erro:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha ao conectar ao Stripe: {erro}",
            ) from erro
    try:
        result = response.json()
    except ValueError as erro:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="O Stripe retornou uma resposta inválida.",
        ) from erro
    if response.is_error:
        error = result.get("error", {}) if isinstance(result, dict) else {}
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error.get("message", "O Stripe recusou a operação.")
            if isinstance(error, dict)
            else "O Stripe recusou a operação.",
        )
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="O Stripe retornou uma resposta inválida.",
        )
    return result


def _verify_webhook(payload: bytes, signature: str | None, secret: str) -> dict:
    if signature is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assinatura do webhook ausente.",
        )
    parts = {}
    for item in signature.split(","):
        key, separator, value = item.partition("=")
        if separator:
            parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError, IndexError) as erro:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assinatura do webhook inválida.",
        ) from erro
    if abs(time.time() - timestamp) > 300:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assinatura do webhook expirada.",
        )
    signed_payload = str(timestamp).encode() + b"." + payload
    expected = hmac.new(
        secret.encode(), signed_payload, hashlib.sha256
    ).hexdigest()
    if not any(
        hmac.compare_digest(expected, candidate)
        for candidate in parts.get("v1", [])
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assinatura do webhook inválida.",
        )
    try:
        event = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as erro:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conteúdo do webhook inválido.",
        ) from erro
    if not isinstance(event, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conteúdo do webhook inválido.",
        )
    return event


def _period_end(timestamp: Any) -> str | None:
    if not isinstance(timestamp, int):
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _subscription_status(stripe_status: str) -> str:
    return {
        "incomplete": "pending",
        "incomplete_expired": "expired",
        "trialing": "trialing",
        "active": "active",
        "past_due": "past_due",
        "unpaid": "past_due",
        "canceled": "cancelled",
        "paused": "expired",
    }.get(stripe_status, "pending")


def create_billing_router(
    get_settings: Callable[[], Settings],
    get_authenticated_user: Callable[..., Awaitable[dict]],
    request_supabase: Callable[..., Awaitable[httpx.Response]],
    admin_headers: Callable[[Settings], dict[str, str]],
) -> APIRouter:
    router = APIRouter(prefix="/v1/account", tags=["account and billing"])

    async def list_user_subscriptions(user_id: str, settings: Settings) -> list[dict]:
        response = await request_supabase(
            "GET",
            f"{settings.supabase_url}/rest/v1/subscriptions",
            admin_headers(settings),
            params={
                "select": (
                    "id,provider,plan,status,current_period_end,created_at,"
                    "provider_subscription_id,metadata"
                ),
                "user_id": f"eq.{user_id}",
                "order": "created_at.desc",
                "limit": "20",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Supabase retornou assinaturas em formato inválido.",
            )
        return rows

    async def has_blocking_subscription(user_id: str, settings: Settings) -> bool:
        response = await request_supabase(
            "GET",
            f"{settings.supabase_url}/rest/v1/subscriptions",
            admin_headers(settings),
            params={
                "select": "id",
                "user_id": f"eq.{user_id}",
                "status": "in.(pending,trialing,active,past_due)",
                "limit": "1",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Supabase retornou assinaturas em formato inválido.",
            )
        return bool(rows)

    async def remove_user_images(user_id: str, settings: Settings) -> None:
        headers = admin_headers(settings)
        files: list[str] = []

        async def collect(prefix: str) -> None:
            offset = 0
            while True:
                response = await request_supabase(
                    "POST",
                    f"{settings.supabase_url}/storage/v1/object/list/user-images",
                    headers,
                    json_body={
                        "prefix": prefix,
                        "limit": 1000,
                        "offset": offset,
                    },
                )
                entries = response.json()
                if not isinstance(entries, list):
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="O Supabase retornou arquivos em formato inválido.",
                    )
                for entry in entries:
                    name = entry.get("name") if isinstance(entry, dict) else None
                    if not isinstance(name, str):
                        continue
                    path = f"{prefix}/{name}"
                    if entry.get("id") is None:
                        await collect(path)
                    else:
                        files.append(path)
                if len(entries) < 1000:
                    return
                offset += len(entries)

        await collect(user_id)
        for start in range(0, len(files), 1000):
            await request_supabase(
                "DELETE",
                f"{settings.supabase_url}/storage/v1/object/user-images",
                headers,
                json_body={"prefixes": files[start : start + 1000]},
            )

    @router.get("/subscriptions")
    async def get_subscriptions(
        user: dict = Depends(get_authenticated_user),
        settings: Settings = Depends(get_settings),
    ) -> list[dict]:
        return await list_user_subscriptions(str(user["id"]), settings)

    @router.post("/checkout")
    async def create_checkout(
        user: dict = Depends(get_authenticated_user),
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        required = (
            settings.stripe_price_id,
            settings.stripe_success_url,
            settings.stripe_cancel_url,
        )
        if not all(required):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="O checkout Stripe ainda não está configurado no servidor.",
            )
        if await has_blocking_subscription(str(user["id"]), settings):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Já existe uma assinatura ou checkout pendente nesta conta.",
            )
        session = await _stripe_request(
            "POST",
            "checkout/sessions",
            settings,
            data={
                "mode": "subscription",
                "line_items[0][price]": settings.stripe_price_id or "",
                "line_items[0][quantity]": "1",
                "success_url": settings.stripe_success_url or "",
                "cancel_url": settings.stripe_cancel_url or "",
                "client_reference_id": str(user["id"]),
                "metadata[user_id]": str(user["id"]),
                "metadata[plan]": "Plano Pro",
                "subscription_data[metadata][user_id]": str(user["id"]),
                "subscription_data[metadata][plan]": "Plano Pro",
            },
        )
        session_id = session.get("id")
        checkout_url = session.get("url")
        if not isinstance(session_id, str) or not isinstance(checkout_url, str):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Stripe não confirmou a criação do checkout.",
            )
        await request_supabase(
            "POST",
            f"{settings.supabase_url}/rest/v1/subscriptions",
            {
                **admin_headers(settings),
                "Prefer": "return=minimal",
            },
            json_body={
                "user_id": str(user["id"]),
                "provider": "stripe",
                "plan": "Plano Pro",
                "status": "pending",
                "metadata": {"checkout_session_id": session_id},
            },
        )
        return {"url": checkout_url}

    @router.post("/subscriptions/{subscription_id}/cancel")
    async def cancel_subscription(
        subscription_id: UUID,
        request: CancelSubscriptionRequest,
        user: dict = Depends(get_authenticated_user),
        settings: Settings = Depends(get_settings),
    ) -> dict:
        rows = await list_user_subscriptions(str(user["id"]), settings)
        subscription = next(
            (item for item in rows if item.get("id") == str(subscription_id)),
            None,
        )
        if subscription is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assinatura não encontrada.",
            )
        if subscription.get("provider") != "stripe":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Esta assinatura não é gerenciada pelo Stripe.",
            )
        stripe_subscription_id = subscription.get("provider_subscription_id")
        if not stripe_subscription_id:
            checkout_id = (subscription.get("metadata") or {}).get(
                "checkout_session_id"
            )
            if not checkout_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Não foi possível localizar o checkout pendente.",
                )
            await _stripe_request(
                "POST",
                f"checkout/sessions/{checkout_id}/expire",
                settings,
            )
            update = {"status": "cancelled"}
        elif request.at_period_end:
            result = await _stripe_request(
                "POST",
                f"subscriptions/{stripe_subscription_id}",
                settings,
                data={"cancel_at_period_end": "true"},
            )
            update = {
                "current_period_end": _period_end(result.get("current_period_end"))
            }
            if update["current_period_end"] is None:
                update.pop("current_period_end")
            metadata = subscription.get("metadata") or {}
            update["metadata"] = {
                **metadata,
                "cancel_at_period_end": True,
            }
        else:
            await _stripe_request(
                "DELETE",
                f"subscriptions/{stripe_subscription_id}",
                settings,
            )
            update = {"status": "cancelled"}

        response = await request_supabase(
            "PATCH",
            f"{settings.supabase_url}/rest/v1/subscriptions",
            {**admin_headers(settings), "Prefer": "return=representation"},
            params={
                "id": f"eq.{subscription_id}",
                "user_id": f"eq.{user['id']}",
            },
            json_body=update,
        )
        rows_updated = response.json()
        if not isinstance(rows_updated, list) or not rows_updated:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="O Stripe confirmou o cancelamento, mas o estado não foi salvo.",
            )
        return rows_updated[0]

    @router.delete("", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_account(
        user: dict = Depends(get_authenticated_user),
        settings: Settings = Depends(get_settings),
    ) -> None:
        if await has_blocking_subscription(str(user["id"]), settings):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cancele a assinatura e aguarde o encerramento do período "
                    "antes de excluir a conta."
                ),
            )
        await remove_user_images(str(user["id"]), settings)
        await request_supabase(
            "DELETE",
            f"{settings.supabase_url}/auth/v1/admin/users/{user['id']}",
            admin_headers(settings),
        )

    @router.post("/stripe/webhook")
    async def stripe_webhook(
        request: Request,
        stripe_signature: str | None = Header(default=None),
        settings: Settings = Depends(get_settings),
    ) -> dict[str, bool]:
        if not settings.stripe_webhook_secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="O webhook Stripe ainda não está configurado.",
            )
        event = _verify_webhook(
            await request.body(), stripe_signature, settings.stripe_webhook_secret
        )
        event_type = event.get("type")
        data = event.get("data")
        subscription = data.get("object") if isinstance(data, dict) else None
        if not isinstance(subscription, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="O webhook não contém um objeto válido.",
            )

        if event_type in {
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        }:
            metadata = subscription.get("metadata") or {}
            user_id = metadata.get("user_id")
            stripe_id = subscription.get("id")
            if not user_id or not stripe_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A assinatura não contém os metadados de conta necessários.",
                )
            try:
                UUID(user_id)
            except (ValueError, TypeError) as erro:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A assinatura contém um identificador de conta inválido.",
                ) from erro
            stripe_status = (
                "canceled"
                if event_type == "customer.subscription.deleted"
                else subscription.get("status", "incomplete")
            )
            body = {
                "user_id": user_id,
                "provider": "stripe",
                "provider_subscription_id": stripe_id,
                "plan": metadata.get("plan", "Plano Pro"),
                "status": _subscription_status(stripe_status),
                "metadata": {
                    "cancel_at_period_end": bool(
                        subscription.get("cancel_at_period_end")
                    ),
                },
            }
            period_end = _period_end(subscription.get("current_period_end"))
            if period_end:
                body["current_period_end"] = period_end
            pending = await request_supabase(
                "PATCH",
                f"{settings.supabase_url}/rest/v1/subscriptions",
                {
                    **admin_headers(settings),
                    "Prefer": "return=representation",
                },
                params={
                    "user_id": f"eq.{user_id}",
                    "provider": "eq.stripe",
                    "status": "eq.pending",
                    "provider_subscription_id": "is.null",
                },
                json_body=body,
            )
            updated_pending = pending.json()
            if not isinstance(updated_pending, list):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Não foi possível vincular o checkout à assinatura.",
                )
            if not updated_pending:
                await request_supabase(
                    "POST",
                    f"{settings.supabase_url}/rest/v1/subscriptions",
                    {
                        **admin_headers(settings),
                        "Prefer": (
                            "resolution=merge-duplicates,return=minimal"
                        ),
                    },
                    params={
                        "on_conflict": "provider,provider_subscription_id"
                    },
                    json_body=body,
                )
        elif event_type == "checkout.session.expired":
            metadata = subscription.get("metadata") or {}
            user_id = metadata.get("user_id")
            session_id = subscription.get("id")
            if user_id and session_id:
                await request_supabase(
                    "PATCH",
                    f"{settings.supabase_url}/rest/v1/subscriptions",
                    {**admin_headers(settings), "Prefer": "return=minimal"},
                    params={
                        "user_id": f"eq.{user_id}",
                        "provider": "eq.stripe",
                        "status": "eq.pending",
                        "metadata->>checkout_session_id": f"eq.{session_id}",
                    },
                    json_body={"status": "expired"},
                )
        return {"received": True}

    return router
