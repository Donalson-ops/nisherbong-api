import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    resend_api_key: str | None
    resend_from_email: str | None
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_id: str | None = None
    stripe_success_url: str | None = None
    stripe_cancel_url: str | None = None

    @classmethod
    def from_environment(cls) -> "Settings":
        required = {
            "SUPABASE_URL": os.environ.get("SUPABASE_URL", "").strip(),
            "SUPABASE_ANON_KEY": os.environ.get(
                "SUPABASE_ANON_KEY",
                "",
            ).strip(),
            "SUPABASE_SERVICE_ROLE_KEY": os.environ.get(
                "SUPABASE_SERVICE_ROLE_KEY",
                "",
            ).strip(),
        }
        ausentes = [nome for nome, valor in required.items() if not valor]
        if ausentes:
            raise RuntimeError(
                "Variáveis obrigatórias não configuradas: "
                + ", ".join(ausentes)
            )
        return cls(
            supabase_url=required["SUPABASE_URL"]
            .rstrip("/")
            .removesuffix("/rest/v1"),
            supabase_anon_key=required["SUPABASE_ANON_KEY"],
            supabase_service_role_key=required[
                "SUPABASE_SERVICE_ROLE_KEY"
            ],
            resend_api_key=os.environ.get("RESEND_API_KEY") or None,
            resend_from_email=os.environ.get("RESEND_FROM_EMAIL") or None,
            stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY") or None,
            stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET") or None,
            stripe_price_id=os.environ.get("STRIPE_PRICE_ID") or None,
            stripe_success_url=os.environ.get("STRIPE_SUCCESS_URL") or None,
            stripe_cancel_url=os.environ.get("STRIPE_CANCEL_URL") or None,
        )
