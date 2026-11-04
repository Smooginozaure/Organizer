"""Scan à blanc : inventaire des mails et pièces jointes SANS rien télécharger.

Seule la structure des mails (BODYSTRUCTURE) est lue : le serveur y indique,
pour chaque partie, son type, son nom de fichier et sa taille. L'inventaire est
enregistré dans l'index local ; c'est lui que la récupération utilise ensuite.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import credentials, db, progression
from .classify import DECO, PJ, QUARANTINE, analyze_bodystructure, extension
from .config import Config, dossier_retenu
from .imap_client import Session
from .imap_parse import envelope_sender, envelope_subject, parse_fetch
from .rapport_html import Barres, Tableau, Tuile, nombre, page, pluriel, taille_lisible

FETCH_ITEMS = "(UID RFC822.SIZE INTERNALDATE ENVELOPE BODYSTRUCTURE)"
LOT = 200
EXCLUS_ESTIMATION = ("corbeille", "spam")
MOIS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
ROLES_LISIBLES = {"réception": "Boîte de réception", "envoyés": "Envoyés", "brouillons": "Brouillons",
                  "corbeille": "Corbeille", "spam": "Spam", "autre": "Autre dossier"}


def date_interne(s) -> datetime | None:
    """INTERNALDATE '17-Jul-1996 02:44:25 -0700' (indépendant de la langue du système)."""
    try:
        jour, mois, reste = str(s).strip().split("-", 2)
        annee, heure = reste.split(" ")[:2]
        h, mi, se = (int(x) for x in heure.split(":"))
        return datetime(int(annee), MOIS[mois.lower()], int(jour), h, mi, se)
    except Exception:
        return None


@dataclass
class StatDossier:
    compte: str
    dossier: str
    brut: str
    role: str
    uidvalidity: str = ""
    messages: int = 0
    volume_messages: int = 0
    messages_avec_pj: int = 0
    pj: int = 0
    volume_pj: int = 0
    images_deco: int = 0
    volume_deco: int = 0
    erreurs: int = 0
    duree_s: float = 0.0


@dataclass
class Resultat:
    debut: datetime
    lignes: list[dict] = field(default_factory=list)
    messages: list[tuple] = field(default_factory=list)
    dossiers: list[StatDossier] = field(default_factory=list)
    erreurs: list[str] = field(default_factory=list)
    limite: int | None = None
    # Scan rapide : seuls les mails arrivés depuis le scan « base » sont analysés.
    mode: str = "complet"
    base: object = None  # ligne `scans` du scan de référence
    nouveaux: int = 0
    supprimes: int = 0
    dossiers_relus: int = 0  # dossiers relus entièrement (nouveaux, ou renumérotés par le serveur)


def _log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # console limitée : on remplace les caractères non affichables
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(msg.encode(enc, "replace").decode(enc), flush=True)


def _precedent(conn, base_id: int, compte: str, brut: str, uidvalidity: str) -> dict | None:
    """Inventaire du dossier au scan de référence : {uid: (message, [pièces])}, ou None s'il faut tout relire."""
    d = conn.execute("SELECT uidvalidity FROM dossiers WHERE scan_id = ? AND compte = ? AND brut = ?",
                     (base_id, compte, brut)).fetchone()
    if d is None or not uidvalidity or (d["uidvalidity"] or "") != uidvalidity:
        return None
    inventaire = {m["uid"]: (m, []) for m in conn.execute(
        "SELECT * FROM messages WHERE scan_id = ? AND compte = ? AND dossier = ?", (base_id, compte, brut))}
    for p in conn.execute("SELECT * FROM pieces WHERE scan_id = ? AND compte = ? AND dossier = ?", (base_id, compte, brut)):
        if p["uid"] in inventaire:
            inventaire[p["uid"]][1].append(p)
    return inventaire


def _reprendre(m, pieces, nom_compte: str, dossier, stat: "StatDossier", resultat: "Resultat") -> None:
    """Reprend tel quel un mail déjà inventorié (scan rapide)."""
    stat.messages += 1
    stat.volume_messages += m["taille"] or 0
    resultat.messages.append((nom_compte, dossier.brut, m["uid"], m["taille"], m["date"], m["adresse"], m["objet"],
                              m["nb_pj"], m["expediteur"] or ""))
    if m["nb_pj"]:
        stat.messages_avec_pj += 1
    date = m["date"] or ""
    for p in pieces:
        if p["statut"] == PJ:
            stat.pj += 1
            stat.volume_pj += p["taille"] or 0
        else:
            stat.images_deco += 1
            stat.volume_deco += p["taille"] or 0
        resultat.lignes.append({
            "compte": nom_compte, "dossier": dossier.nom, "dossier_brut": dossier.brut, "role_dossier": dossier.role,
            "uid": m["uid"], "chemin": p["chemin"], "date": date[:10], "annee": int(date[:4]) if date[:4].isdigit() else "",
            "expediteur": m["expediteur"] or "", "adresse": m["adresse"] or "", "objet": m["objet"] or "",
            "fichier": p["nom"] or "", "extension": extension(p["nom"] or ""), "categorie": p["categorie"] or "",
            "mime": p["mime"] or "", "taille": p["taille"] or 0, "taille_encodee": p["taille_encodee"] or p["taille"] or 0,
            "statut": p["statut"], "doublon": "",
        })


def scanner_compte(compte, resultat: Resultat, limite: int | None, base_conn=None) -> None:
    password = credentials.get_password(compte)
    if not password:
        resultat.erreurs.append(f"{compte.nom} : aucun mot de passe enregistré.")
        progression.r().alerte(f"{compte.nom} : aucun mot de passe enregistré, compte ignoré.")
        return

    progression.r().titre(f"{compte.nom} ({compte.email}) — connexion à {compte.hote}")
    session = Session(compte, password)
    try:
        session.connect()
    except Exception as e:
        resultat.erreurs.append(f"{compte.nom} : {e}")
        progression.r().alerte(str(e))
        return

    try:
        for dossier in session.dossiers():
            if not dossier.selectionnable or dossier.role == "tous":
                continue
            stat = StatDossier(compte=compte.nom, dossier=dossier.nom, brut=dossier.brut, role=dossier.role)
            resultat.dossiers.append(stat)
            t0 = time.monotonic()
            try:
                session.ouvrir(dossier)
                stat.uidvalidity = session.uidvalidity
                uids = session.uids()
            except Exception as e:
                stat.erreurs += 1
                resultat.erreurs.append(f"{compte.nom} / {dossier.nom} : ouverture impossible ({e})")
                continue
            if limite:
                uids = uids[-limite:]
            if base_conn is not None:
                precedent = _precedent(base_conn, resultat.base["id"], compte.nom, dossier.brut, stat.uidvalidity)
                if precedent is None:
                    resultat.dossiers_relus += 1
                else:
                    courants = {int(u) for u in uids}
                    for uid, (m, ps) in precedent.items():
                        if uid in courants:
                            _reprendre(m, ps, compte.nom, dossier, stat, resultat)
                        else:
                            resultat.supprimes += 1  # supprimé ou déplacé depuis le scan précédent
                    uids = [u for u in uids if int(u) not in precedent]
                    resultat.nouveaux += len(uids)
            progression.r().dossier(dossier.nom, len(uids), "" if base_conn is None else " nouveaux")

            for i in range(0, len(uids), LOT):
                lot = uids[i : i + LOT]
                try:
                    reponses = parse_fetch(session.fetch(lot, FETCH_ITEMS))
                except Exception as e:
                    stat.erreurs += len(lot)
                    resultat.erreurs.append(f"{compte.nom} / {dossier.nom} : lot {i}-{i + len(lot)} illisible ({e})")
                    continue
                for msg in reponses:
                    _analyser_message(msg, compte.nom, dossier, stat, resultat)
                progression.r().avance(len(lot))
            progression.r().fin_dossier()
            stat.duree_s = time.monotonic() - t0
    finally:
        session.close()


def _analyser_message(msg: dict, nom_compte: str, dossier, stat: StatDossier, resultat: Resultat) -> None:
    stat.messages += 1
    try:
        taille = int(msg.get("RFC822.SIZE") or 0)
    except ValueError:
        taille = 0
    stat.volume_messages += taille
    try:
        uid = int(msg.get("UID"))
    except (TypeError, ValueError):
        stat.erreurs += 1
        return
    date = date_interne(msg.get("INTERNALDATE"))
    env = msg.get("ENVELOPE")
    nom_exp, adr_exp = envelope_sender(env)
    objet = envelope_subject(env)
    try:
        pieces = analyze_bodystructure(msg.get("BODYSTRUCTURE"))
    except Exception:
        pieces = []
        stat.erreurs += 1

    nb_pj = sum(1 for p in pieces if p.statut == PJ)
    resultat.messages.append((nom_compte, dossier.brut, uid, taille, date.isoformat() if date else "", adr_exp, objet, nb_pj, nom_exp))
    if nb_pj:
        stat.messages_avec_pj += 1

    for p in pieces:
        if p.statut == PJ:
            stat.pj += 1
            stat.volume_pj += p.taille
        else:
            stat.images_deco += 1
            stat.volume_deco += p.taille
        resultat.lignes.append(
            {
                "compte": nom_compte,
                "dossier": dossier.nom,
                "dossier_brut": dossier.brut,
                "role_dossier": dossier.role,
                "uid": uid,
                "chemin": p.chemin,
                "date": date.strftime("%Y-%m-%d") if date else "",
                "annee": date.year if date else "",
                "expediteur": nom_exp,
                "adresse": adr_exp,
                "objet": objet,
                "fichier": p.nom,
                "extension": p.extension,
                "categorie": p.categorie,
                "mime": p.mime,
                "taille": p.taille,
                "taille_encodee": p.taille_encodee,
                "statut": p.statut,
                "doublon": "",
            }
        )


def _marquer_doublons(lignes: list[dict]) -> None:
    """Doublon probable = même nom de fichier et même taille exacte (déjà vu ailleurs)."""
    vus = set()
    # Les dossiers utiles d'abord : l'original doit être compté hors corbeille/spam.
    for l in sorted(lignes, key=lambda l: l["role_dossier"] in EXCLUS_ESTIMATION):
        if l["statut"] != PJ:
            continue
        cle = (l["fichier"].lower(), l["taille"])
        if cle in vus:
            l["doublon"] = "oui"
        else:
            vus.add(cle)


def _somme(lignes) -> int:
    return sum(l["taille"] for l in lignes)


def _groupes(lignes, cle) -> dict:
    g: dict = defaultdict(list)
    for l in lignes:
        g[cle(l)].append(l)
    return g


class Synthese:
    """Tous les chiffres du rapport, calculés une seule fois."""

    def __init__(self, res: Resultat, retenu=None):
        self.res = res
        pj = [l for l in res.lignes if l["statut"] == PJ]
        self.deco = [l for l in res.lignes if l["statut"] == DECO]
        self.utiles = [l for l in pj if l["role_dossier"] not in EXCLUS_ESTIMATION]
        self.uniques = [l for l in self.utiles if l["doublon"] != "oui"]
        self.corbeille_spam = [l for l in pj if l["role_dossier"] in EXCLUS_ESTIMATION]
        self.vol_boites = sum(d.volume_messages for d in res.dossiers)
        self.nb_mails = sum(d.messages for d in res.dossiers)
        self.nb_mails_pj = sum(d.messages_avec_pj for d in res.dossiers)
        self.dossiers_recup = [d for d in res.dossiers if retenu and retenu(d.role, d.dossier)]
        self.vol_mails_recup = sum(d.volume_messages for d in self.dossiers_recup)
        self.nb_mails_recup = sum(d.messages for d in self.dossiers_recup)
        cles_recup = {(d.compte, d.brut) for d in self.dossiers_recup}
        self.pj_recup = [l for l in self.uniques if (l["compte"], l["dossier_brut"]) in cles_recup]
        self.comptes = sorted({d.compte for d in res.dossiers})


def resume_texte(s: Synthese, sauvegarde_mails: bool) -> str:
    res = s.res
    L: list[str] = []
    L.append("RAPPORT DE SCAN À BLANC — INVENTAIRE DES BOÎTES MAIL")
    L.append(f"Réalisé le {res.debut:%d/%m/%Y à %H:%M} — aucun fichier téléchargé, aucun mail modifié ni supprimé.")
    if res.mode == "incremental":
        L.append(mention_rapide(res))
    if res.limite:
        L.append(f"⚠ Scan PARTIEL : seulement les {res.limite} derniers mails de chaque dossier (non utilisable pour la récupération).")
    L.append("")
    L.append("── EN RÉSUMÉ ──")
    L.append(f"  Vos boîtes contiennent {nombre(s.nb_mails)} mails pour {taille_lisible(s.vol_boites)}.")
    L.append(f"  {nombre(s.nb_mails_pj)} mails contiennent au moins une pièce jointe.")
    L.append(f"  Pièces jointes utiles, sans doublons, hors corbeille/spam : {taille_lisible(_somme(s.uniques))} ({pluriel(len(s.uniques), 'fichier')}).")
    L.append("")
    L.append("── CE QUE LA RÉCUPÉRATION VA TÉLÉCHARGER (selon vos réglages) ──")
    L.append(f"  Dossiers retenus : {', '.join(sorted({ROLES_LISIBLES.get(d.role, d.role) for d in s.dossiers_recup})) or 'aucun'}")
    L.append(f"  Pièces jointes à classer : {taille_lisible(_somme(s.pj_recup))} ({pluriel(len(s.pj_recup), 'fichier')})")
    if sauvegarde_mails:
        L.append(f"  Copie des mails (.eml)   : {taille_lisible(s.vol_mails_recup)} ({nombre(s.nb_mails_recup)} mails)")
        L.append(f"  Espace disque total      : ~{taille_lisible(_somme(s.pj_recup) + s.vol_mails_recup)}")
    L.append("")
    L.append("── DÉTAIL DE L'ESTIMATION ──")
    L.append(f"  Avec les doublons probables                       : {taille_lisible(_somme(s.utiles))}  ({pluriel(len(s.utiles), 'fichier')})")
    L.append(f"  Pièces jointes en corbeille / spam (non comptées) : {taille_lisible(_somme(s.corbeille_spam))}  ({pluriel(len(s.corbeille_spam), 'fichier')})")
    L.append(f"  Images décoratives écartées (logos, signatures)   : {taille_lisible(_somme(s.deco))}  ({pluriel(len(s.deco), 'image')})")

    for compte in s.comptes:
        ds = [d for d in res.dossiers if d.compte == compte]
        u = [l for l in s.uniques if l["compte"] == compte]
        L.append("")
        L.append(f"── COMPTE {compte.upper()} ── {nombre(sum(d.messages for d in ds))} mails "
                 f"({taille_lisible(sum(d.volume_messages for d in ds))}) — {pluriel(len(u), 'PJ utile')} = {taille_lisible(_somme(u))}")
        for d in sorted(ds, key=lambda d: -d.volume_pj):
            if d.messages == 0:
                continue
            note = " (exclu de l'estimation)" if d.role in EXCLUS_ESTIMATION else ""
            L.append(f"      {d.dossier:<32} {d.messages:>7} mails  {d.pj:>6} PJ  {taille_lisible(d.volume_pj):>10}{note}")

    def tableau(titre, groupes, top=None, tri_cle=False):
        items = sorted(groupes.items(), key=(lambda kv: str(kv[0])) if tri_cle else (lambda kv: -_somme(kv[1])))
        L.append("")
        L.append(f"── {titre} ──")
        for k, ls in items[:top] if top else items:
            L.append(f"  {str(k) or '(inconnu)':<44} {len(ls):>6} fichiers  {taille_lisible(_somme(ls)):>10}")

    tableau("PAR TYPE DE FICHIER", _groupes(s.uniques, lambda l: l["categorie"]))
    tableau("PAR ANNÉE", _groupes(s.uniques, lambda l: l["annee"]), tri_cle=True)
    tableau("TOP 20 EXPÉDITEURS (par volume)", _groupes(s.uniques, lambda l: l["adresse"] or l["expediteur"]), top=20)

    L.append("")
    L.append("── LES 15 PLUS GROS FICHIERS ──")
    for l in sorted(s.uniques, key=lambda l: -l["taille"])[:15]:
        L.append(f"  {taille_lisible(l['taille']):>10}  {l['date']}  {l['fichier'][:60]}  ({l['compte']} / {l['adresse']})")

    quarantaine = [l for l in s.uniques if l["categorie"] == QUARANTINE]
    if quarantaine:
        L.append("")
        L.append(f"⚠ {len(quarantaine)} fichier(s) exécutable(s)/script(s) détecté(s) : ils iront en quarantaine, jamais ouverts.")
    if res.erreurs:
        L.append("")
        L.append("── INCIDENTS ──")
        L.extend(f"  {e}" for e in res.erreurs)
    return "\n".join(L)


def rapport_html(s: Synthese, sauvegarde_mails: bool) -> str:
    res = s.res
    alertes = []
    if res.mode == "incremental":
        alertes.append(mention_rapide(res))
    if res.limite:
        alertes.append(f"Scan partiel ({res.limite} derniers mails par dossier) : chiffres incomplets, non utilisable pour la récupération.")
    alertes += res.erreurs[:20]

    total_recup = _somme(s.pj_recup) + (s.vol_mails_recup if sauvegarde_mails else 0)
    tuiles = [
        Tuile(taille_lisible(total_recup), "Espace disque nécessaire pour la récupération",
              f"{taille_lisible(_somme(s.pj_recup))} de pièces jointes"
              + (f" + {taille_lisible(s.vol_mails_recup)} de copies de mails" if sauvegarde_mails else "")),
        Tuile(nombre(s.nb_mails), "mails dans vos boîtes", f"{taille_lisible(s.vol_boites)} au total"),
        Tuile(nombre(s.nb_mails_pj), "mails avec pièce jointe", f"{pluriel(len(s.uniques), 'pièce jointe utile', 'pièces jointes utiles')}"),
        Tuile(taille_lisible(_somme(s.uniques)), "de pièces jointes utiles",
              "sans doublons, hors corbeille et spam"),
    ]

    def barres(titre, groupes, tri_cle=False, top=None, note=""):
        items = sorted(groupes.items(), key=(lambda kv: str(kv[0])) if tri_cle else (lambda kv: -_somme(kv[1])))
        if top:
            items = items[:top]
        return Barres(titre, [(str(k) or "(inconnu)", _somme(v), len(v)) for k, v in items], note)

    blocs = [
        Tableau(
            "Comptes",
            ["Compte", "Mails", "Taille de la boîte", "Mails avec PJ", "PJ utiles", "Volume PJ utiles"],
            [[c, nombre(sum(d.messages for d in res.dossiers if d.compte == c)),
              taille_lisible(sum(d.volume_messages for d in res.dossiers if d.compte == c)),
              nombre(sum(d.messages_avec_pj for d in res.dossiers if d.compte == c)),
              nombre(len([l for l in s.uniques if l["compte"] == c])),
              taille_lisible(_somme([l for l in s.uniques if l["compte"] == c]))] for c in s.comptes],
            numeriques={1, 2, 3, 4, 5},
        ),
        barres("Pièces jointes utiles par type de fichier", _groupes(s.uniques, lambda l: l["categorie"]),
               note="Volume sur disque, sans doublons, hors corbeille et spam. Le nombre de fichiers suit le point."),
        barres("Pièces jointes utiles par année", _groupes(s.uniques, lambda l: l["annee"]), tri_cle=True),
        barres("Les 20 expéditeurs qui envoient le plus de pièces jointes", _groupes(s.uniques, lambda l: l["adresse"] or l["expediteur"]), top=20),
        Tableau(
            "Détail par dossier",
            ["Compte", "Dossier", "Rôle", "Mails", "Taille", "PJ", "Volume PJ", "Récupéré"],
            [[d.compte, d.dossier, ROLES_LISIBLES.get(d.role, d.role), nombre(d.messages), taille_lisible(d.volume_messages),
              nombre(d.pj), taille_lisible(d.volume_pj), "oui" if d in s.dossiers_recup else "non"]
             for d in sorted(res.dossiers, key=lambda d: (d.compte, -d.volume_messages)) if d.messages],
            note="« Récupéré » : dossiers inclus dans la récupération selon vos réglages (config.json → recuperation).",
            numeriques={3, 4, 5, 6},
        ),
        Tableau(
            "Les 15 plus gros fichiers",
            ["Taille", "Date", "Fichier", "Compte", "Expéditeur"],
            [[taille_lisible(l["taille"]), l["date"], l["fichier"], l["compte"], l["adresse"]]
             for l in sorted(s.uniques, key=lambda l: -l["taille"])[:15]],
            numeriques={0},
        ),
        Tableau(
            "Ce qui est écarté",
            ["Élément", "Fichiers", "Volume"],
            [["Doublons probables (même nom, même taille)", nombre(len(s.utiles) - len(s.uniques)), taille_lisible(_somme(s.utiles) - _somme(s.uniques))],
             ["Pièces jointes en corbeille et spam", nombre(len(s.corbeille_spam)), taille_lisible(_somme(s.corbeille_spam))],
             ["Images décoratives (logos, signatures, newsletters)", nombre(len(s.deco)), taille_lisible(_somme(s.deco))]],
            numeriques={1, 2},
        ),
    ]
    quarantaine = [l for l in s.uniques if l["categorie"] == QUARANTINE]
    if quarantaine:
        alertes.append(f"{len(quarantaine)} fichier(s) exécutable(s) ou script(s) détecté(s) : ils iront en quarantaine et ne seront jamais ouverts.")
    return page(
        "Rapport de scan à blanc",
        f"Réalisé le {res.debut:%d/%m/%Y à %H:%M} — rien n'a été téléchargé, aucun mail n'a été modifié ni supprimé.",
        tuiles, blocs, alertes,
    )


def ecrire_rapport(res: Resultat, dossier: Path, cfg: Config) -> tuple[Path, str]:
    dossier.mkdir(parents=True, exist_ok=True)
    colonnes = ["compte", "dossier", "date", "annee", "expediteur", "adresse", "objet", "fichier", "extension",
                "categorie", "mime", "taille", "statut", "doublon", "role_dossier", "uid", "chemin"]
    with open(dossier / "inventaire_pieces_jointes.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=colonnes, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(res.lignes)

    with open(dossier / "dossiers.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["compte", "dossier", "role", "mails", "volume_mails", "mails_avec_pj", "pj", "volume_pj",
                    "images_decoratives", "volume_decoratives", "erreurs", "duree_s"])
        for d in res.dossiers:
            w.writerow([d.compte, d.dossier, d.role, d.messages, d.volume_messages, d.messages_avec_pj, d.pj,
                        d.volume_pj, d.images_deco, d.volume_deco, d.erreurs, round(d.duree_s, 1)])

    sauvegarde = bool(cfg.recuperation.get("sauvegarder_mails"))
    s = Synthese(res, retenu=lambda role, nom: dossier_retenu(cfg.recuperation, role, nom))
    texte = resume_texte(s, sauvegarde)
    (dossier / "rapport.txt").write_text(texte + "\n", encoding="utf-8-sig")
    (dossier / "rapport.html").write_text(rapport_html(s, sauvegarde), encoding="utf-8")
    (dossier / "resume.json").write_text(json.dumps({
        "date": res.debut.isoformat(timespec="seconds"),
        "partiel": bool(res.limite),
        "mode": res.mode,
        "nouveaux": res.nouveaux,
        "supprimes": res.supprimes,
        "mails": s.nb_mails,
        "volume_boites_octets": s.vol_boites,
        "pj_utiles_octets": _somme(s.uniques),
        "recuperation_pj_octets": _somme(s.pj_recup),
        "recuperation_mails_octets": s.vol_mails_recup if sauvegarde else 0,
        "erreurs": res.erreurs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return dossier, texte


def mention_rapide(res: Resultat) -> str:
    base = datetime.fromisoformat(res.base["date"]) if res.base else None
    texte = (f"Scan rapide : seuls les mails arrivés depuis le scan du {base:%d/%m/%Y à %H:%M} ont été analysés — "
             f"{res.nouveaux} nouveau(x), {res.supprimes} supprimé(s) ou déplacé(s) depuis.")
    if res.dossiers_relus:
        texte += f" {res.dossiers_relus} dossier(s) nouveau(x) ou renuméroté(s) relu(s) entièrement."
    return texte


def enregistrer_index(res: Resultat, cfg: Config, rapport: Path) -> int:
    conn = db.ouvrir(cfg.base_index)
    with conn:
        cur = conn.execute(
            "INSERT INTO scans(date, partiel, termine, rapport, mode, base_id, nouveaux, supprimes) VALUES (?, ?, 0, ?, ?, ?, ?, ?)",
            (res.debut.isoformat(timespec="seconds"), 1 if res.limite else 0, str(rapport), res.mode,
             res.base["id"] if res.base else None, res.nouveaux if res.mode == "incremental" else None,
             res.supprimes if res.mode == "incremental" else None))
        scan_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO dossiers (scan_id, compte, brut, nom, role, uidvalidity, messages) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(scan_id, d.compte, d.brut, d.dossier, d.role, d.uidvalidity, d.messages) for d in res.dossiers],
        )
        conn.executemany("INSERT OR IGNORE INTO messages (scan_id, compte, dossier, uid, taille, date, adresse, objet, nb_pj, "
                         "expediteur) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [(scan_id, *m) for m in res.messages])
        conn.executemany(
            "INSERT OR IGNORE INTO pieces (scan_id, compte, dossier, uid, chemin, nom, categorie, taille, statut, mime, "
            "taille_encodee) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(scan_id, l["compte"], l["dossier_brut"], l["uid"], l["chemin"], l["fichier"], l["categorie"], l["taille"],
              l["statut"], l["mime"], l["taille_encodee"]) for l in res.lignes],
        )
        # Un compte inaccessible rend le scan incomplet : il ne peut pas servir de base à la récupération.
        complet = not any("connexion" in e.lower() or "mot de passe" in e.lower() for e in res.erreurs)
        conn.execute("UPDATE scans SET termine = ? WHERE id = ?", (1 if complet else 0, scan_id))
    conn.close()
    return scan_id


def lancer(cfg: Config, comptes_filtre: list[str] | None = None, limite: int | None = None, ouvrir: bool = True,
           afficher_resume: bool = True, rapide: bool = False) -> Path:
    """rapide=True : n'analyse que les mails arrivés depuis le dernier scan (le reste est repris de l'inventaire)."""
    res = Resultat(debut=datetime.now(), limite=limite)
    base_conn = None
    if rapide and not limite and not comptes_filtre:
        base_conn = db.ouvrir(cfg.base_index)
        res.base = db.dernier_scan_complet(base_conn)
        if res.base is None:  # premier scan : il faut tout lire
            base_conn.close()
            base_conn = None
        else:
            res.mode = "incremental"
    comptes = [c for c in cfg.comptes if not comptes_filtre or c.nom.lower() in {x.lower() for x in comptes_filtre}]
    if not comptes:
        _log("Aucun compte configuré.")
    try:
        for compte in comptes:
            scanner_compte(compte, res, limite, base_conn)
    finally:
        if base_conn is not None:
            base_conn.close()
    _marquer_doublons(res.lignes)
    dossier = cfg.dossier_rapports / f"scan_a_blanc_{res.debut:%Y-%m-%d_%H%M%S}"
    n = 2
    while dossier.exists():  # jamais écraser un rapport existant
        dossier = dossier.with_name(f"scan_a_blanc_{res.debut:%Y-%m-%d_%H%M%S}_{n}")
        n += 1
    chemin, texte = ecrire_rapport(res, dossier, cfg)
    enregistrer_index(res, cfg, chemin / "rapport.html")
    if afficher_resume:
        _log("\n" + texte)
        _log(f"\nRapport complet : {chemin / 'rapport.html'}")
    if ouvrir:
        ouvrir_fichier(chemin / "rapport.html")
    return chemin


def ouvrir_fichier(p: Path) -> None:
    try:
        import os
        os.startfile(str(p))  # type: ignore[attr-defined]
    except Exception:
        pass
