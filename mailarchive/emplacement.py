"""Changement d'emplacement de l'archive (Bureau, Documents, disque externe…), sans rien perdre.

- Même disque : simple renommage du dossier (instantané).
- Autre disque : copie complète, vérification (nombre et taille des fichiers), puis bascule.
  L'ancienne copie est CONSERVÉE : à vous de la supprimer une fois la nouvelle vérifiée.
Les chemins enregistrés dans l'index sont mis à jour dans tous les cas.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from .config import load_raw, save_raw

COLONNES_CHEMINS = [("fichiers", "chemin_local"), ("pieces_recuperees", "chemin_local"),
                    ("mails_recuperes", "fichier_eml"), ("scans", "rapport")]


def _inventaire(racine: Path) -> tuple[int, int]:
    n = taille = 0
    for p in racine.rglob("*"):
        if p.is_file():
            n += 1
            taille += p.stat().st_size
    return n, taille


def _meme_disque(a: Path, b: Path) -> bool:
    return os.path.splitdrive(str(a.resolve()))[0].lower() == os.path.splitdrive(str(b.resolve()))[0].lower()


def _maj_index(index: Path, ancien: Path, nouveau: Path) -> None:
    a, n = str(ancien), str(nouveau)
    conn = sqlite3.connect(index)
    with conn:
        for table, col in COLONNES_CHEMINS:
            conn.execute(f"UPDATE {table} SET {col} = ? || substr({col}, ?) WHERE substr({col}, 1, ?) = ?",
                         (n, len(a) + 1, len(a), a))
    conn.close()


def deplacer_archive(ancien: Path, nouveau: Path) -> str:
    ancien, nouveau = ancien.resolve(), Path(os.path.expandvars(str(nouveau))).expanduser().resolve()
    if os.path.normcase(str(ancien)) == os.path.normcase(str(nouveau)):
        return "L'archive est déjà à cet emplacement."
    if ancien in nouveau.parents or nouveau in ancien.parents:
        raise ValueError("Le nouvel emplacement ne peut pas être à l'intérieur de l'ancien (ni l'inverse).")
    if nouveau.exists() and any(nouveau.iterdir()):
        raise ValueError(f"{nouveau} existe déjà et n'est pas vide : choisissez un dossier nouveau ou vide.")
    nouveau.parent.mkdir(parents=True, exist_ok=True)

    if not ancien.exists():
        message = "Nouvel emplacement enregistré (aucune archive à déplacer)."
    elif _meme_disque(ancien, nouveau):
        if nouveau.exists():
            nouveau.rmdir()
        os.replace(ancien, nouveau)
        _maj_index(nouveau / "_index" / "archive.sqlite", ancien, nouveau)
        message = f"Archive déplacée vers {nouveau}."
    else:
        avant = _inventaire(ancien)
        libre = shutil.disk_usage(nouveau.parent).free
        if libre < avant[1] * 1.05:
            raise ValueError(f"Espace insuffisant sur le disque de destination ({libre // 2**20} Mo libres).")
        shutil.copytree(ancien, nouveau, dirs_exist_ok=True)
        apres = _inventaire(nouveau)
        if apres != avant:
            raise RuntimeError(f"Copie incomplète ({apres[0]}/{avant[0]} fichiers) : l'ancienne archive reste utilisée.")
        _maj_index(nouveau / "_index" / "archive.sqlite", ancien, nouveau)
        message = (f"Archive copiée et vérifiée vers {nouveau} ({avant[0]} fichiers). "
                   f"L'ancienne copie ({ancien}) est conservée : supprimez-la vous-même quand vous voulez.")
    raw = load_raw()
    raw["dossier_archive"] = str(nouveau)
    save_raw(raw)
    return message
