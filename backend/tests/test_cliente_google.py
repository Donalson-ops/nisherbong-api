import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import cliente_conta


class LoginGoogleTests(unittest.TestCase):
    def test_fluxo_google_recebe_codigo_local_e_troca_por_sessao(self) -> None:
        chamadas: list[tuple[str, str, dict | None]] = []

        def falso_chamar(metodo, caminho, corpo=None, token=None):
            chamadas.append((metodo, caminho, corpo))
            if caminho.startswith("/v1/auth/google/url"):
                return {"url": "https://exemplo.supabase.co/auth/v1/authorize"}
            return {"access_token": "acesso", "refresh_token": "renovacao"}

        def abrir_navegador(_url: str) -> bool:
            def retornar() -> None:
                time.sleep(0.3)
                urllib.request.urlopen(
                    f"http://127.0.0.1:{cliente_conta.PORTA_GOOGLE}/callback?code=codigo-123456"
                ).read()

            threading.Thread(target=retornar, daemon=True).start()
            return True

        with tempfile.TemporaryDirectory() as pasta, patch.object(
            cliente_conta, "ARQUIVO_SESSAO", Path(pasta) / "sessao.json"
        ), patch.object(cliente_conta, "_chamar", falso_chamar), patch(
            "webbrowser.open", abrir_navegador
        ):
            sessao = cliente_conta.entrar_com_google()
            salva = cliente_conta.ARQUIVO_SESSAO.exists()

        self.assertEqual(sessao["access_token"], "acesso")
        self.assertTrue(salva)
        troca = chamadas[-1]
        self.assertEqual(troca[1], "/v1/auth/google/exchange")
        self.assertEqual(troca[2]["auth_code"], "codigo-123456")
        self.assertGreaterEqual(len(troca[2]["code_verifier"]), 43)


if __name__ == "__main__":
    unittest.main()
