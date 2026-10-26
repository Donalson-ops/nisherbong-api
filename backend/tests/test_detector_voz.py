import unittest

import numpy as np

from detector_voz import DetectorDeVoz

TAXA = 16000
BLOCO = 1024


def bloco(amplitude: float) -> bytes:
    ruido = np.random.default_rng(1).normal(0, amplitude, BLOCO)
    return np.clip(ruido, -32768, 32767).astype(np.int16).tobytes()


class Relogio:
    def __init__(self) -> None:
        self.agora = 0.0

    def __call__(self) -> float:
        return self.agora


class DetectorDeVozTests(unittest.TestCase):
    def criar(self, falando=lambda: True):
        self.chamadas = 0
        self.relogio = Relogio()

        def ao_detectar() -> None:
            self.chamadas += 1

        return DetectorDeVoz(ao_detectar, falando, TAXA, relogio=self.relogio)

    def alimentar(self, detector, amplitude: float, blocos: int) -> None:
        for _ in range(blocos):
            self.relogio.agora += BLOCO / TAXA
            detector.processar(bloco(amplitude))

    def test_eco_constante_do_alto_falante_nao_interrompe(self) -> None:
        detector = self.criar()
        self.alimentar(detector, 1500, 80)
        self.assertEqual(self.chamadas, 0)

    def test_fala_acima_do_eco_interrompe(self) -> None:
        detector = self.criar()
        self.alimentar(detector, 1500, 15)
        self.alimentar(detector, 6000, 10)
        self.assertGreaterEqual(self.chamadas, 1)

    def test_ruido_curto_nao_interrompe(self) -> None:
        detector = self.criar()
        self.alimentar(detector, 200, 15)
        self.alimentar(detector, 6000, 1)
        self.alimentar(detector, 200, 15)
        self.assertEqual(self.chamadas, 0)

    def test_nada_dispara_se_a_ia_nao_esta_falando(self) -> None:
        detector = self.criar(falando=lambda: False)
        self.alimentar(detector, 8000, 40)
        self.assertEqual(self.chamadas, 0)

    def test_silencio_nao_interrompe(self) -> None:
        detector = self.criar()
        self.alimentar(detector, 50, 60)
        self.assertEqual(self.chamadas, 0)


if __name__ == "__main__":
    unittest.main()
