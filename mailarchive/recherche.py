"""Recherche d'un document dans l'archive (nom, objet du mail, correspondant, type, domaine, année)."""
from __future__ import annotations

from pathlib import Path

from . import db
from .config import Config
from .themes import normaliser


def rechercher(cfg: Config, requete: str, limite: int = 40) -> list[dict]:
    mots = [m for m in normaliser(requete).split() if m]
    if not mots:
        return []
    conn = db.ouvrir(cfg.base_index)
    lignes = conn.execute(
        "SELECT f.chemin_local, f.type_doc, f.theme, f.annee, f.taille, p.nom_origine, p.correspondant, p.objet, p.date_mail "
        "FROM fichiers f LEFT JOIN pieces_recuperees p ON p.sha256 = f.sha256 ORDER BY p.date_mail DESC"
    ).fetchall()
    conn.close()
    vus, res = set(), []
    for l in lignes:
        if l["chemin_local"] in vus:
            continue
        texte = normaliser(" ".join(str(l[k] or "") for k in ("nom_origine", "correspondant", "objet", "type_doc", "theme", "annee")))
        if all(f" {m}" in texte for m in mots):  # début de mot : « fact » trouve « facture »
            vus.add(l["chemin_local"])
            res.append(dict(l))
            if len(res) >= limite:
                break
    return res


def montrer_dans_explorateur(chemin: str) -> None:
    import subprocess

    if Path(chemin).exists():
        subprocess.Popen(["explorer", "/select,", chemin])
