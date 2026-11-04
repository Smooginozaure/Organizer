"""Tri assisté du dossier « À classer » par Claude, dans une conversation.

1. `tri-export` prépare, EN LOCAL, le seul fichier que Claude a le droit de lire :
   Archive mail/_tri_assiste/a_trier.json. En sont exclus, sans exception :
   - tout document confidentiel (gendarmerie, diplomatie : voir confidentiel.py),
     y compris les doublons d'un document confidentiel ;
   - tout document échangé UNIQUEMENT entre particuliers (adresses Gmail, iCloud,
     Free… y compris les vôtres) : c'est par là que circulent les documents de
     travail envoyés depuis des adresses personnelles (règle de prudence) ;
   - tout document dont le texte n'est pas lisible localement (scans, images,
     mails joints) : son contenu ne peut pas être vérifié, il reste à trier par vous.
   Avant chaque export, le filtre (à jour) est réappliqué à toute l'archive.
2. Claude écrit ses propositions dans decisions.json, que vous validez.
3. `tri-appliquer decisions.json` déplace les fichiers (journal pour annuler),
   en refusant tout document confidentiel ou hors « À classer ».
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from . import db, extract
from .classify import extension
from .config import Config
from email.parser import BytesHeaderParser

from .confidentiel import Filtre, echange_entre_particuliers
from .recuperation import deplacer_fichier, marquer_confidentiel, nom_sur, participants
from .themes import A_CLASSER, A_CLASSER_CONF, AUTRES, REGLES_PATH, TRI_MANUEL

TEXTE_MINIMUM = 80
EXTRAIT = 1500


def dossier_tri(cfg: Config) -> Path:
    return cfg.dossier_archive / "_tri_assiste"


def _participants(conn, o) -> list[str] | None:
    """Adresses de tous les participants du mail d'origine, lues dans la copie .eml locale (None si absente)."""
    r = conn.execute(
        "SELECT fichier_eml FROM mails_recuperes WHERE compte = ? AND dossier = ? AND uidvalidity = ? AND uid = ?",
        (o["compte"], o["dossier"], o["uidvalidity"], o["uid"]),
    ).fetchone()
    if not r or not r["fichier_eml"] or not Path(r["fichier_eml"]).exists():
        return None
    with open(r["fichier_eml"], "rb") as fh:
        entetes = BytesHeaderParser().parse(fh)
    return participants(entetes)


def _occurrences(conn, f):
    return conn.execute(
        "SELECT compte, dossier, uidvalidity, uid, nom_origine, objet, correspondant, date_mail, confidentiel "
        "FROM pieces_recuperees WHERE sha256 = ?",
        (f["sha256"],),
    ).fetchall()


def reverifier(cfg: Config) -> int:
    """Réapplique le filtre de confidentialité (à jour) à toute l'archive, en local.

    Un fichier devenu confidentiel est marqué comme tel ; s'il était « À classer », il passe
    dans « À classer - confidentiel ». Retourne le nombre de fichiers nouvellement marqués.
    """
    conn = db.ouvrir(cfg.base_index)
    filtre = Filtre(cfg.raw.get("confidentiel"))
    nouveaux = 0
    for f in conn.execute("SELECT rowid AS id, * FROM fichiers WHERE confidentiel = 0").fetchall():
        occ = _occurrences(conn, f)
        adresses: list[str] = []
        for o in occ:
            adresses += _participants(conn, o) or []
        chemin = Path(f["chemin_local"])
        noms = [o["nom_origine"] or "" for o in occ] or [chemin.name]
        texte = extract.texte(chemin.read_bytes(), extension(noms[0])) if chemin.exists() else ""
        raison = filtre.verdict(adresses, *noms, *(o["objet"] or "" for o in occ), texte)
        if not raison and not any(o["confidentiel"] for o in occ):
            continue
        marquer_confidentiel(conn, f["sha256"], raison)
        if chemin.exists():
            deplacer_fichier(conn, cfg, f["compte"], f["sha256"])
        nouveaux += 1
    conn.close()
    return nouveaux


def exporter(cfg: Config) -> Path:
    nouveaux_conf = reverifier(cfg)
    conn = db.ouvrir(cfg.base_index)
    filtre = Filtre(cfg.raw.get("confidentiel"))
    sortie = dossier_tri(cfg)
    sortie.mkdir(parents=True, exist_ok=True)
    exclus_conf = exclus_texte = exclus_absents = exclus_particuliers = 0
    elements = []
    # Candidats : type ou domaine encore inconnu, et pas déjà validé à la main.
    for f in conn.execute("SELECT rowid AS id, * FROM fichiers WHERE type_manuel = 0 AND (theme = ? OR type_doc = ?) "
                          "AND COALESCE(type_doc, '') != ? ORDER BY compte, chemin_local", (A_CLASSER, AUTRES, TRI_MANUEL)):
        occ = _occurrences(conn, f)
        if f["confidentiel"] or any(o["confidentiel"] for o in occ):
            exclus_conf += 1
            continue
        chemin = Path(f["chemin_local"])
        if not chemin.exists():
            exclus_absents += 1
            continue
        # Prudence : il faut connaître TOUS les participants de CHAQUE mail d'origine, et qu'au moins
        # un correspondant soit une organisation (pas uniquement des particuliers).
        listes = [_participants(conn, o) for o in occ]
        if not occ or any(l is None for l in listes) or any(echange_entre_particuliers(l) for l in listes):
            exclus_particuliers += 1
            continue
        adresses = [a for l in listes for a in l]
        noms = [o["nom_origine"] or "" for o in occ]
        objets = [o["objet"] or "" for o in occ]
        texte = extract.texte(chemin.read_bytes(), extension(noms[0]))
        if len(texte.strip()) < TEXTE_MINIMUM:
            exclus_texte += 1
            continue
        if filtre.verdict(adresses, *noms, *objets, *(o["correspondant"] or "" for o in occ), texte):
            exclus_conf += 1  # double vérification au moment de l'export
            continue
        o = occ[0]
        elements.append({
            "id": f["id"],
            "compte": f["compte"],
            "fichier": noms[0],
            "date": o["date_mail"],
            "correspondant": o["correspondant"],
            "objets": sorted(set(x for x in objets if x))[:3],
            "extrait": texte[:EXTRAIT],
        })

    regles = json.loads(REGLES_PATH.read_text(encoding="utf-8"))
    themes = sorted({r["theme"] for r in regles.get("regles", [])})
    export = sortie / "a_trier.json"
    export.write_text(json.dumps({
        "date": datetime.now().isoformat(timespec="seconds"),
        "consigne": "Proposer un type et un domaine par id. Un nouveau domaine « Domaine/Sous-domaine » est possible.",
        "types_existants": sorted({t["type"] for t in regles.get("types", [])}),
        "domaines_existants": themes,
        "elements": elements,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    conn.close()
    # Liste lisible, pour vérifier vous-même avant toute transmission à Claude.
    with open(sortie / "A_VERIFIER_avant_envoi_a_Claude.txt", "w", encoding="utf-8-sig") as fv:
        fv.write(f"Documents que Claude lirait pour le tri assisté ({len(elements)}). "
                 "Vérifiez qu'aucun ne concerne la gendarmerie ou la diplomatie.\n\n")
        for e in elements:
            fv.write(f"{e['compte']} | {e['date']} | {e['fichier']} | {e['correspondant']} | {' / '.join(e['objets'])}\n")
    consigne = sortie / "CONSIGNE_pour_agent_IA.txt"
    consigne.write_text(CONSIGNE.format(export=export, decisions=sortie / "decisions.json"), encoding="utf-8-sig")
    print(f"Export prêt : {export}")
    if nouveaux_conf:
        print(f"  {nouveaux_conf} fichiers de l'archive nouvellement reconnus confidentiels (filtre mis à jour)")
    print(f"  {len(elements)} documents exportés pour le tri assisté")
    print(f"  {exclus_conf} documents confidentiels exclus (gendarmerie / diplomatie) — restent en tri local")
    print(f"  {exclus_particuliers} documents échangés uniquement entre particuliers exclus (prudence) — à trier par vous")
    print(f"  {exclus_texte} documents sans texte lisible exclus (scans, images, mails joints) — à trier par vous")
    if exclus_absents:
        print(f"  {exclus_absents} fichiers introuvables sur le disque (déplacés à la main ?)")
    return export


CONSIGNE = """Tu aides à classer des documents d'archive personnels.

1. Lis le fichier JSON : {export}
   Il contient une liste « elements » (id, fichier, date, correspondant, objets du mail, extrait du texte),
   la liste « types_existants » et la liste « domaines_existants ».
2. Pour CHAQUE élément, choisis :
   - « type » : la nature du document, de préférence dans types_existants (Factures, Tickets de caisse, Contrats…) ;
   - « domaine » : son sujet, de préférence dans domaines_existants (ex. « Énergie/Électricité - Gaz ») ;
     un nouveau domaine « Domaine/Sous-domaine » est possible s'il est vraiment plus juste.
   Si tu n'es pas sûr, n'inclus pas l'élément.
3. Écris le résultat, et RIEN d'autre, dans ce fichier : {decisions}
   au format : {{"decisions": [{{"id": 12, "type": "Factures", "domaine": "Énergie/Électricité - Gaz"}}, …]}}
4. Ne modifie, ne déplace et ne supprime aucun autre fichier. Montre-moi un résumé de tes choix.
"""


def appliquer(cfg: Config, decisions_path: Path, autoriser_confidentiel: bool = False) -> int:
    """Décisions : [{"id": 12, "type": "Factures", "domaine": "Énergie/Électricité - Gaz"}, …]."""
    conn = db.ouvrir(cfg.base_index)
    decisions = json.loads(Path(decisions_path).read_text(encoding="utf-8"))
    decisions = decisions.get("decisions", decisions) if isinstance(decisions, dict) else decisions
    journal_path = dossier_tri(cfg) / f"journal_{datetime.now():%Y-%m-%d_%H%M%S}.csv"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    faits = refuses = 0
    with open(journal_path, "w", newline="", encoding="utf-8-sig") as jf:
        w = csv.writer(jf, delimiter=";")
        w.writerow(["id", "compte", "ancien emplacement", "nouvel emplacement", "ancien type", "ancien domaine", "type", "domaine"])
        for d in decisions:
            fid = d.get("id")
            type_doc = (d.get("type") or "").strip()
            dom = (d.get("domaine") or d.get("theme") or "").strip().strip("/")
            f = conn.execute("SELECT rowid AS id, * FROM fichiers WHERE rowid = ?", (fid,)).fetchone()
            if f is None or not type_doc:
                refuses += 1
                continue
            if f["confidentiel"] and not autoriser_confidentiel:  # seule l'IA locale (hors ligne) y a droit
                print(f"  Refusé : id {fid} (document confidentiel : tri local uniquement)")
                refuses += 1
                continue
            dom = "/".join(nom_sur(x) for x in dom.split("/") if x.strip()) or A_CLASSER
            ancien = f["chemin_local"]
            nouveau = deplacer_fichier(conn, cfg, f["compte"], f["sha256"], theme=dom, type_doc=nom_sur(type_doc), manuel=True)
            w.writerow([fid, f["compte"], ancien, nouveau, f["type_doc"], f["theme"], type_doc, dom])
            faits += 1
    conn.close()
    print(f"{faits} fichiers reclassés, {refuses} refusés. Journal (pour annuler) : {journal_path}")
    return 0 if refuses == 0 else 1


def annuler(cfg: Config, journal: Path) -> int:
    """Remet les fichiers d'un journal dans leur type et domaine d'avant."""
    conn = db.ouvrir(cfg.base_index)
    n = 0
    with open(journal, encoding="utf-8-sig") as jf:
        for row in csv.DictReader(jf, delimiter=";"):
            f = conn.execute("SELECT * FROM fichiers WHERE rowid = ?", (int(row["id"]),)).fetchone()
            if f and Path(f["chemin_local"]).exists():
                deplacer_fichier(conn, cfg, f["compte"], f["sha256"], theme=row["ancien domaine"] or A_CLASSER,
                                 type_doc=row["ancien type"] or AUTRES)
                with conn:
                    conn.execute("UPDATE fichiers SET type_manuel = 0 WHERE rowid = ?", (int(row["id"]),))
                n += 1
    conn.close()
    print(f"{n} fichiers remis à leur place précédente.")
    return 0
