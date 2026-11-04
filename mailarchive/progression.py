"""Affichage de l'avancement (texte simple par défaut ; l'interface remplace par des barres de progression)."""
from __future__ import annotations

import sys


def _print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(msg.encode(enc, "replace").decode(enc), flush=True)


class Rapporteur:
    """Version texte, utilisée en ligne de commande et par la veille automatique."""

    def __init__(self):
        self._total = 0
        self._fait = 0

    def titre(self, texte: str) -> None:
        _print(f"\n=== {texte} ===")

    def alerte(self, texte: str) -> None:
        _print(f"⚠ {texte}")

    def dossier(self, nom: str, total: int, detail: str = "") -> None:
        self._total, self._fait = total, 0
        _print(f"  • {nom} : {total} mails{detail}")

    def avance(self, n: int) -> None:
        self._fait += n
        if self._total > 200:
            _print(f"      {self._fait}/{self._total}")

    def fin_dossier(self) -> None:
        pass

    def texte(self, texte: str) -> None:
        _print(texte)


class Silencieux(Rapporteur):
    def titre(self, texte):
        pass

    def dossier(self, nom, total, detail=""):
        pass

    def avance(self, n):
        pass

    def texte(self, texte):
        pass


ACTUEL: Rapporteur = Rapporteur()


def utiliser(r: Rapporteur) -> Rapporteur:
    global ACTUEL
    ancien, ACTUEL = ACTUEL, r
    return ancien


def r() -> Rapporteur:
    return ACTUEL
