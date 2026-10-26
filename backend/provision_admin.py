"""Cria (ou atualiza) a conta administradora já confirmada no Supabase.

Uso:
    python -m backend.provision_admin --email admin@exemplo.com
A senha é lida de ADMIN_PASSWORD ou solicitada no terminal.
"""

import argparse
import getpass
import os
import sys

import httpx

from backend.settings import Settings


def provisionar(email: str, senha: str, settings: Settings) -> str:
    cabecalhos = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
    }
    base = settings.supabase_url
    with httpx.Client(timeout=20, headers=cabecalhos) as cliente:
        resposta = cliente.post(
            f"{base}/auth/v1/admin/users",
            json={"email": email, "password": senha, "email_confirm": True},
        )
        if resposta.status_code in (400, 409, 422):
            listagem = cliente.get(
                f"{base}/auth/v1/admin/users", params={"per_page": "1000"}
            )
            listagem.raise_for_status()
            existente = next(
                (
                    u
                    for u in listagem.json().get("users", [])
                    if (u.get("email") or "").lower() == email.lower()
                ),
                None,
            )
            if existente is None:
                raise SystemExit(f"Falha ao criar o usuário: {resposta.text}")
            resposta = cliente.put(
                f"{base}/auth/v1/admin/users/{existente['id']}",
                json={"password": senha, "email_confirm": True},
            )
        resposta.raise_for_status()
        usuario_id = resposta.json()["id"]
        perfil = cliente.post(
            f"{base}/rest/v1/profiles",
            params={"on_conflict": "id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json={"id": usuario_id, "role": "admin"},
        )
        perfil.raise_for_status()
    return usuario_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default=os.environ.get("ADMIN_EMAIL"))
    args = parser.parse_args()
    if not args.email:
        sys.exit("Informe --email ou ADMIN_EMAIL.")
    senha = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Senha do admin: ")
    if len(senha) < 8:
        sys.exit("A senha deve ter ao menos 8 caracteres.")
    usuario_id = provisionar(args.email, senha, Settings.from_environment())
    print(f"Administrador pronto: {args.email} ({usuario_id})")


if __name__ == "__main__":
    main()
