"""Réorganisation de l'archive existante : <Type>/<Domaine>/<Sous-domaine>/<Année>, toutes boîtes confondues.

- Type et domaine sont recalculés en local avec les règles à jour (sauf décisions validées à la main).
- Un fichier identique présent dans plusieurs boîtes n'est gardé qu'une fois.
- Les copies de mails (.eml) vont dans _Mails sauvegardés/<Boîte>/…
- Mode simulation : affiche le résultat sans rien déplacer. Journal CSV de chaque déplacement.
"""
from __future__ import annotations

import csv
import hashlib
import os
from collections import Counter
from datetime import datetime
from email.parser import BytesHeaderParser
from pathlib import Path

from . import db, extract
from .classify import QUARANTINE, category, extension
from .config import Config
from .recuperation import DOSSIER_MAILS, _nettoyer, deplacer_fichier, nom_sur
from .themes import Classeur, arborescence, TRI_MANUEL


def _annee(f, occ, chemin: Path) -> str:
    if f["annee"]:
        return f["annee"]
    for o in occ:
        d = (o["date_mail"] or "")[:4]
        if d.isdigit():
            return d
    return chemin.parent.name


def _adresse_correspondant(conn, o, propres: set[str]) -> str:
    """Adresse de l'autre partie (expéditeur, ou destinataire pour un mail envoyé), lue dans la copie .eml."""
    r = conn.execute(
        "SELECT fichier_eml FROM mails_recuperes WHERE compte = ? AND dossier = ? AND uidvalidity = ? AND uid = ?",
        (o["compte"], o["dossier"], o["uidvalidity"], o["uid"]),
    ).fetchone()
    if not r or not r["fichier_eml"] or not Path(r["fichier_eml"]).exists():
        return ""
    from .recuperation import participants

    with open(r["fichier_eml"], "rb") as fh:
        adresses = participants(BytesHeaderParser().parse(fh))
    autres = [a for a in adresses if a not in propres]
    return (autres or adresses or [""])[0]


def _sha_fichier(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for bloc in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def deplacer_copies_mails(cfg: Config, conn) -> int:
    """Archive mail/<Boîte>/_Mails sauvegardés → Archive mail/_Mails sauvegardés/<Boîte>."""
    n = 0
    for compte in cfg.comptes:
        src = cfg.dossier_archive / nom_sur(compte.nom) / DOSSIER_MAILS
        dst = cfg.dossier_archive / DOSSIER_MAILS / nom_sur(compte.nom)
        if not src.exists():
            continue
        if dst.exists():
            raise RuntimeError(f"{dst} existe déjà : déplacement des copies de mails annulé.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        ancien, nouveau = str(src), str(dst)
        with conn:
            conn.execute("UPDATE mails_recuperes SET fichier_eml = ? || substr(fichier_eml, ?) WHERE compte = ? AND fichier_eml LIKE ?",
                         (nouveau, len(ancien) + 1, compte.nom, ancien + "%"))
        n += 1
    return n


def supprimer_dossiers_vides(racine: Path) -> None:
    for d in sorted((p for p in racine.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()  # n'échoue silencieusement que si le dossier n'est pas vide
        except OSError:
            pass


def reorganiser(cfg: Config, simulation: bool = False) -> Path | None:
    conn = db.ouvrir(cfg.base_index)
    classeur = Classeur.charger(exclusions=cfg.raw.get("exclusions"))
    propres = {c.email.lower() for c in cfg.comptes}
    plan: Counter = Counter()
    gardes: dict[str, str] = {}  # sha256 -> chemin du fichier conservé
    deplaces = doublons = absents = 0
    journal = None
    if not simulation:
        journal = cfg.dossier_rapports / f"reorganisation_{datetime.now():%Y-%m-%d_%H%M%S}.csv"
        journal.parent.mkdir(parents=True, exist_ok=True)
        jf = open(journal, "w", newline="", encoding="utf-8-sig")
        w = csv.writer(jf, delimiter=";")
        w.writerow(["compte", "ancien emplacement", "nouvel emplacement", "type", "domaine"])

    for f in conn.execute("SELECT rowid AS id, * FROM fichiers ORDER BY rowid").fetchall():
        chemin = Path(f["chemin_local"])
        if not chemin.exists():
            absents += 1
            continue
        occ = conn.execute(
            "SELECT compte, dossier, uidvalidity, uid, nom_origine, objet, date_mail FROM pieces_recuperees WHERE sha256 = ?",
            (f["sha256"],),
        ).fetchall()
        annee = _annee(f, occ, chemin)
        nom = (occ[0]["nom_origine"] if occ else "") or chemin.name
        objets = " / ".join(sorted({o["objet"] or "" for o in occ}))
        cat = category(extension(nom), "")

        if f["type_manuel"]:
            type_doc, theme = f["type_doc"], f["theme"]
        else:
            texte = "" if cat in ("Images / photos", "Vidéos", "Audio", QUARANTINE) else extract.texte(chemin.read_bytes(), extension(nom))
            type_doc = classeur.classer_type(nom, objets, texte, cat).theme
            adresse = _adresse_correspondant(conn, occ[0], propres) if occ else ""
            theme = classeur.classer(nom, objets, adresse, texte, cat).theme
            if (f["theme"] not in ("À classer", "À classer - confidentiel") and theme in ("À classer", "Photos et vidéos")
                    and type_doc != TRI_MANUEL and not classeur.touche_ignores(nom, objets, texte)):
                theme = f["theme"]  # ne pas perdre un domaine déjà reconnu (sauf si un mot exclu en était la cause)

        parties = arborescence(type_doc, theme, annee, bool(f["confidentiel"]))
        plan[" / ".join(parties[:-1])] += 1

        # Même fichier déjà gardé (autre boîte) : on ne conserve qu'un exemplaire.
        if f["sha256"] in gardes and gardes[f["sha256"]] != str(chemin):
            doublons += 1
            if not simulation and _sha_fichier(chemin) == f["sha256"] and Path(gardes[f["sha256"]]).exists():
                chemin.unlink()
                _nettoyer(chemin.parent, cfg.dossier_archive)
                with conn:
                    conn.execute("UPDATE fichiers SET chemin_local = ?, theme = ?, type_doc = ?, annee = ? WHERE rowid = ?",
                                 (gardes[f["sha256"]], theme, type_doc, annee, f["id"]))
                    conn.execute("UPDATE pieces_recuperees SET chemin_local = ? WHERE sha256 = ?", (gardes[f["sha256"]], f["sha256"]))
                w.writerow([f["compte"], str(chemin), gardes[f["sha256"]] + " (doublon supprimé)", type_doc, theme])
            continue

        if simulation:
            gardes[f["sha256"]] = str(chemin)
            continue
        with conn:
            conn.execute("UPDATE fichiers SET annee = ? WHERE rowid = ?", (annee, f["id"]))
        nouveau = deplacer_fichier(conn, cfg, f["compte"], f["sha256"], theme=theme, type_doc=type_doc)
        gardes[f["sha256"]] = nouveau
        if nouveau != str(chemin):
            deplaces += 1
            w.writerow([f["compte"], str(chemin), nouveau, type_doc, theme])

    if not simulation:
        jf.close()
        n = deplacer_copies_mails(cfg, conn)
        for compte in cfg.comptes:
            ancien = cfg.dossier_archive / nom_sur(compte.nom)
            if ancien.exists():
                supprimer_dossiers_vides(ancien)
                try:
                    ancien.rmdir()
                except OSError:
                    pass
        print(f"{deplaces} fichiers déplacés, {doublons} doublons entre boîtes supprimés (copies identiques), "
              f"copies de mails de {n} boîte(s) regroupées dans « {DOSSIER_MAILS} ».")
        print(f"Journal : {journal}")
    else:
        print(f"SIMULATION — rien n'a été déplacé. {doublons} doublons entre boîtes seraient fusionnés.")
    if absents:
        print(f"{absents} fichiers introuvables sur le disque (ignorés).")
    print("\nArborescence obtenue (nombre de fichiers) :")
    for k, v in sorted(plan.items()):
        print(f"  {v:>4}  {k}")
    conn.close()
    return journal
