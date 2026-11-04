"""Récupération : sauvegarde des mails (.eml) et extraction + classement des pièces jointes.

S'appuie OBLIGATOIREMENT sur le dernier scan à blanc complet : seuls les mails
inventoriés par ce scan sont récupérés. Les mails restent intacts sur le
serveur (connexion en lecture seule, voir imap_client.py). Chaque mail traité
est noté dans l'index : une récupération interrompue reprend où elle s'était arrêtée.
"""
from __future__ import annotations

import csv
import email
import email.policy
import hashlib
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import getaddresses
from pathlib import Path

from . import credentials, db, extract, progression
from .classify import QUARANTINE, category, extension
from .config import Compte, Config, dossier_retenu
from .imap_client import Dossier, Session
from .imap_parse import parse_fetch
from .rapport_html import Barres, Tableau, Tuile, nombre, page, pluriel, taille_lisible
from .scan import ROLES_LISIBLES, _log, ouvrir_fichier
from .confidentiel import Filtre
from .themes import A_CLASSER, A_CLASSER_CONF, Classeur, arborescence

DOSSIER_MAILS = "_Mails sauvegardés"
LOT_MAX_OCTETS = 15_000_000
LOT_MAX_MAILS = 25
INTERDITS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
NOMS_RESERVES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


# ───────────────────────── noms de fichiers ─────────────────────────

def nom_sur(texte: str, maxi: int = 80) -> str:
    """Nom de fichier/dossier valide sous Windows."""
    t = INTERDITS.sub("_", texte or "").strip()
    t = re.sub(r"\s+", " ", t).strip(" .")
    if len(t) > maxi:
        t = t[:maxi].rstrip(" .")
    if not t or t.split(".")[0].lower() in NOMS_RESERVES:
        t = f"_{t}" if t else "sans nom"
    return t


def chemin_libre(dossier: Path, base: str, ext: str) -> Path:
    suffixe = f".{ext}" if ext else ""
    p = dossier / f"{base}{suffixe}"
    n = 2
    while p.exists():
        p = dossier / f"{base} ({n}){suffixe}"
        n += 1
    return p


def ecrire(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, p)


# ───────────────────────── lecture des mails ─────────────────────────

def _entete(msg, nom: str) -> str:
    v = msg.get(nom)
    if v is None:
        return ""
    try:
        return str(make_header(decode_header(str(v))))
    except Exception:
        return str(v)


def participants(msg) -> list[str]:
    """Toutes les adresses du mail : expéditeur, destinataires, copies, réponse."""
    champs = [_entete(msg, h) for h in ("From", "Sender", "Reply-To", "To", "Cc", "Bcc")]
    try:
        return [a.lower() for _n, a in getaddresses([c for c in champs if c]) if a]
    except Exception:
        return []


def correspondant(msg, role: str) -> tuple[str, str]:
    """(libellé court, adresse) : l'expéditeur, ou le destinataire pour les mails envoyés."""
    champ = "To" if role in ("envoyés", "brouillons") else "From"
    try:
        adresses = getaddresses([_entete(msg, champ)])
    except Exception:
        adresses = []
    nom, adr = adresses[0] if adresses else ("", "")
    libelle = (nom or adr.split("@")[0] or "inconnu").strip().strip('"')
    if champ == "To":
        libelle = "à " + libelle
    return libelle, adr.lower()


def parties_numerotees(msg, chemin: str = ""):
    """Parties du mail avec la même numérotation que le BODYSTRUCTURE IMAP (1, 1.2, 2…)."""
    if msg.get_content_maintype() == "multipart" and isinstance(msg.get_payload(), list):
        for i, sous in enumerate(msg.get_payload(), 1):
            yield from parties_numerotees(sous, f"{chemin}.{i}" if chemin else str(i))
    else:
        yield (chemin or "1"), msg


def contenu_partie(part) -> bytes | None:
    if part.get_content_maintype() == "message":
        charge = part.get_payload()
        if isinstance(charge, list) and charge:
            try:
                return charge[0].as_bytes()
            except Exception:
                return charge[0].as_string().encode("utf-8", "replace")
        return None
    return part.get_payload(decode=True)


# ───────────────────────── statistiques ─────────────────────────

@dataclass
class Bilan:
    debut: datetime
    mails_sauvegardes: int = 0
    octets_mails: int = 0
    pj_classees: int = 0
    octets_pj: int = 0
    doublons: int = 0
    a_classer: int = 0
    confidentiels: int = 0
    mails_disparus: int = 0
    deja_faits: int = 0
    par_theme: Counter = field(default_factory=Counter)
    octets_theme: Counter = field(default_factory=Counter)
    par_compte: Counter = field(default_factory=Counter)
    erreurs: list[str] = field(default_factory=list)
    nouveaux: list[dict] = field(default_factory=list)


# ───────────────────────── traitement ─────────────────────────

class Recuperateur:
    def __init__(self, cfg: Config, conn, scan_id: int, classeur: Classeur, bilan: Bilan,
                 sauvegarder: bool | None = None, depuis: str | None = None):
        self.cfg = cfg
        self.conn = conn
        self.scan_id = scan_id
        self.classeur = classeur
        self.bilan = bilan
        # True : mail complet (.eml) + pièces jointes ; False : pièces jointes uniquement.
        self.sauvegarder = bool(cfg.recuperation.get("sauvegarder_mails")) if sauvegarder is None else sauvegarder
        self.depuis = depuis  # « AAAA-MM-JJ » : ignorer les mails plus anciens
        self.filtre = Filtre(cfg.raw.get("confidentiel"))

    def compte(self, compte: Compte) -> None:
        dossiers = [
            d for d in self.conn.execute("SELECT * FROM dossiers WHERE scan_id = ? AND compte = ?", (self.scan_id, compte.nom))
            if dossier_retenu(self.cfg.recuperation, d["role"], d["nom"])
        ]
        if not dossiers:
            return
        password = credentials.get_password(compte)
        if not password:
            self.bilan.erreurs.append(f"{compte.nom} : aucun mot de passe enregistré.")
            return
        progression.r().titre(f"{compte.nom} ({compte.email})")
        session = Session(compte, password)
        try:
            session.connect()
        except Exception as e:
            self.bilan.erreurs.append(f"{compte.nom} : {e}")
            progression.r().alerte(str(e))
            return
        try:
            for d in dossiers:
                try:
                    self.dossier(session, compte, d)
                except Exception as e:
                    self.bilan.erreurs.append(f"{compte.nom} / {d['nom']} : interrompu ({e})")
                    progression.r().alerte(f"{d['nom']} interrompu : {e}")
        finally:
            session.close()

    def dossier(self, session: Session, compte: Compte, d) -> None:
        session.ouvrir(Dossier(brut=d["brut"], nom=d["nom"], drapeaux=[]))
        if session.uidvalidity != (d["uidvalidity"] or ""):
            self.bilan.erreurs.append(
                f"{compte.nom} / {d['nom']} : le serveur a renuméroté ce dossier depuis le scan — relancez un scan à blanc."
            )
            return
        filtre = "" if self.sauvegarder else " AND nb_pj > 0"
        params = [self.scan_id, compte.nom, d["brut"]]
        if self.depuis:
            filtre += " AND date >= ?"
            params.append(self.depuis)
        messages = {
            m["uid"]: m for m in self.conn.execute(
                f"SELECT * FROM messages WHERE scan_id = ? AND compte = ? AND dossier = ?{filtre} ORDER BY uid", params)
        }
        # uid -> copie .eml déjà faite ? (un mail traité en « pièces jointes uniquement » n'a pas de copie)
        traites = {
            r["uid"]: bool(r["fichier_eml"]) for r in self.conn.execute(
                "SELECT uid, fichier_eml FROM mails_recuperes WHERE compte = ? AND dossier = ? AND uidvalidity = ?",
                (compte.nom, d["brut"], d["uidvalidity"]),
            )
        }
        self.pj_faites = {u for u in traites}
        a_faire = [uid for uid in messages if uid not in traites or (self.sauvegarder and not traites[uid])]
        self.bilan.deja_faits += len(messages) - len(a_faire)
        deja = len(messages) - len(a_faire)
        progression.r().dossier(d["nom"], len(a_faire), f" à récupérer ({deja} déjà faits)" if deja else " à récupérer")
        if not a_faire:
            progression.r().fin_dossier()
            return

        pieces = defaultdict(dict)
        for p in self.conn.execute(
            "SELECT uid, chemin, nom, categorie FROM pieces WHERE scan_id = ? AND compte = ? AND dossier = ? AND statut = 'pièce jointe'",
            (self.scan_id, compte.nom, d["brut"]),
        ):
            pieces[p["uid"]][p["chemin"]] = p

        fait, t0 = 0, time.monotonic()
        for lot in self._lots(a_faire, messages):
            data = session.fetch([str(u) for u in lot], "(UID BODY.PEEK[])")
            recus = {}
            for rep in parse_fetch(data, raw_literals=True):
                brut = rep.get("BODY[]")
                try:
                    recus[int(rep.get("UID"))] = brut if isinstance(brut, bytes) else (brut or "").encode("utf-8", "replace")
                except (TypeError, ValueError):
                    continue
            for uid in lot:
                if uid not in recus:
                    self.bilan.mails_disparus += 1  # supprimé du serveur depuis le scan
                    continue
                try:
                    self.mail(compte, d, messages[uid], recus[uid], pieces.get(uid, {}))
                except Exception as e:
                    self.bilan.erreurs.append(f"{compte.nom} / {d['nom']} / mail {uid} : {e}")
            fait += len(lot)
            progression.r().avance(len(lot))
        progression.r().fin_dossier()

    @staticmethod
    def _lots(uids: list[int], messages: dict):
        lot, octets = [], 0
        for uid in uids:
            taille = messages[uid]["taille"] or 0
            if lot and (octets + taille > LOT_MAX_OCTETS or len(lot) >= LOT_MAX_MAILS):
                yield lot
                lot, octets = [], 0
            lot.append(uid)
            octets += taille
        if lot:
            yield lot

    def mail(self, compte: Compte, d, m, brut: bytes, pieces: dict) -> None:
        msg = email.message_from_bytes(brut, policy=email.policy.compat32)
        try:
            date = datetime.fromisoformat(m["date"]) if m["date"] else None
        except ValueError:
            date = None
        annee = str(date.year) if date else "Sans date"
        jour = date.strftime("%Y-%m-%d") if date else "sans-date"
        libelle, adresse_corr = correspondant(msg, d["role"])
        objet = _entete(msg, "Subject") or m["objet"] or ""
        base_compte = self.cfg.dossier_archive

        fichier_eml = None
        if self.sauvegarder:
            dossier_mail = ROLES_LISIBLES.get(d["role"], d["nom"]) if d["role"] != "autre" else d["nom"]
            heure = date.strftime("%Hh%M") if date else ""
            nom = nom_sur(f"{jour} {heure} - {libelle[:40]} - {objet[:70] or 'sans objet'}", 150)
            cible = chemin_libre(self.cfg.dossier_archive / DOSSIER_MAILS / nom_sur(compte.nom) / nom_sur(dossier_mail) / annee, nom, "eml")
            ecrire(cible, brut)
            fichier_eml = str(cible)
            self.bilan.mails_sauvegardes += 1
            self.bilan.octets_mails += len(brut)

        if pieces and m["uid"] not in self.pj_faites:  # pièces jointes déjà extraites lors d'un passage précédent
            extraites = []
            for chemin, part in parties_numerotees(msg):
                info = pieces.get(chemin)
                if info is None:
                    continue
                try:
                    contenu = contenu_partie(part)
                except Exception as e:
                    contenu = None
                    self.bilan.erreurs.append(f"{compte.nom} / mail {m['uid']} / partie {chemin} : illisible ({e})")
                if not contenu:
                    continue
                nom_origine = info["nom"] or "sans nom"
                ext = extension(nom_origine)
                cat = info["categorie"] or category(ext, "")
                texte = extract.texte(contenu, ext) if cat not in ("Images / photos", "Vidéos", "Audio", QUARANTINE) else ""
                extraites.append((chemin, info, contenu, nom_origine, ext, cat, texte))

            # Confidentialité (gendarmerie, diplomatie) : décidée pour le mail ENTIER, en local.
            raison = self.filtre.verdict(participants(msg), objet) or next(
                (r for r in (self.filtre.texte(e[3], e[6]) for e in extraites) if r), None)
            for e in extraites:
                self.piece(compte, d, m, *e, base_compte, annee, jour, libelle, adresse_corr, objet, raison)

        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO mails_recuperes VALUES (?, ?, ?, ?, ?, ?)",
                (compte.nom, d["brut"], d["uidvalidity"], m["uid"], fichier_eml, datetime.now().isoformat(timespec="seconds")),
            )

    def piece(self, compte, d, m, chemin, info, contenu: bytes, nom_origine, ext, cat, texte,
              base: Path, annee, jour, libelle, adresse_corr, objet, raison):
        empreinte = hashlib.sha256(contenu).hexdigest()
        conf = 1 if raison else 0
        # Un même fichier n'est stocké qu'une fois, toutes boîtes confondues.
        existant = self.conn.execute(
            "SELECT compte, chemin_local, theme, confidentiel FROM fichiers WHERE sha256 = ? ORDER BY rowid LIMIT 1", (empreinte,)
        ).fetchone()
        if existant and Path(existant["chemin_local"]).exists():
            self.bilan.doublons += 1
            local, theme = existant["chemin_local"], existant["theme"]
            if conf and not existant["confidentiel"]:
                # Le même fichier apparaît dans un mail confidentiel : il le devient aussi.
                marquer_confidentiel(self.conn, empreinte, raison)
                local = deplacer_fichier(self.conn, self.cfg, existant["compte"], empreinte)
            self._noter(compte, d, m, chemin, nom_origine, empreinte, local, theme, 1, jour, libelle, objet, conf)
            return

        decision = self.classeur.classer(nom_origine, objet, adresse_corr, texte, cat)
        theme = decision.theme
        type_doc = self.classeur.classer_type(nom_origine, objet, texte, cat).theme

        dossier = base.joinpath(*[nom_sur(x) for x in arborescence(type_doc, theme, annee, bool(conf))])
        nom_base = nom_sur(f"{jour} - {libelle[:35]} - {Path(nom_origine).stem}", 140)
        ext_final = ext + ".quarantaine" if cat == QUARANTINE else ext
        cible = chemin_libre(dossier, nom_base, ext_final)
        ecrire(cible, contenu)

        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO fichiers (compte, sha256, chemin_local, theme, taille, confidentiel, raison_confidentiel, "
                "type_doc, annee) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (compte.nom, empreinte, str(cible), theme, len(contenu), conf, raison, type_doc, annee))
        self._noter(compte, d, m, chemin, nom_origine, empreinte, str(cible), theme, 0, jour, libelle, objet, conf)
        b = self.bilan
        b.pj_classees += 1
        b.octets_pj += len(contenu)
        cle = f"{type_doc} › {theme}"
        b.par_theme[cle] += 1
        b.octets_theme[cle] += len(contenu)
        b.par_compte[compte.nom] += 1
        b.confidentiels += conf
        if theme in (A_CLASSER, A_CLASSER_CONF):
            b.a_classer += 1
        b.nouveaux.append({"compte": compte.nom, "theme": f"{type_doc} / {theme}", "fichier": str(cible), "origine": nom_origine,
                           "date": jour, "correspondant": libelle, "objet": objet,
                           "indices": "; ".join(decision.indices[:4]), "confidentiel": "oui" if conf else ""})

    def _noter(self, compte, d, m, chemin, nom_origine, empreinte, local, theme, doublon, jour, libelle, objet, conf):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO pieces_recuperees (compte, dossier, uidvalidity, uid, chemin, nom_origine, sha256, "
                "chemin_local, theme, doublon, date_mail, correspondant, objet, confidentiel) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (compte.nom, d["brut"], d["uidvalidity"], m["uid"], chemin, nom_origine, empreinte, local, theme,
                 doublon, jour, libelle, objet, conf),
            )


def _nom_ext(nom: str) -> tuple[str, str]:
    suffixe = ""
    if nom.endswith(".quarantaine"):
        nom, suffixe = nom[: -len(".quarantaine")], ".quarantaine"
    base, point, ext = nom.rpartition(".")
    if not point or not base:
        return nom, suffixe.lstrip(".")
    return base, ext + suffixe


def marquer_confidentiel(conn, sha256: str, raison: str | None) -> None:
    with conn:
        conn.execute("UPDATE fichiers SET confidentiel = 1, raison_confidentiel = ? WHERE sha256 = ?",
                     (raison or "doublon d'un document confidentiel", sha256))
        conn.execute("UPDATE pieces_recuperees SET confidentiel = 1 WHERE sha256 = ?", (sha256,))


def deplacer_fichier(conn, cfg: Config, compte: str, sha256: str, theme: str | None = None,
                     type_doc: str | None = None, manuel: bool = False) -> str:
    """Range un fichier archivé à sa place dans l'arborescence <Type>/<Domaine>/<Année> et met l'index à jour.

    theme / type_doc : nouvelles valeurs (None = inchangées). manuel : décision validée par vous,
    que les réorganisations automatiques ne remettront pas en cause.
    """
    r = conn.execute("SELECT * FROM fichiers WHERE compte = ? AND sha256 = ?", (compte, sha256)).fetchone()
    ancien = Path(r["chemin_local"])
    theme = theme if theme is not None else r["theme"]
    type_doc = type_doc if type_doc is not None else (r["type_doc"] or "Autres documents")
    annee = r["annee"] or ancien.parent.name
    parties = arborescence(type_doc, theme, annee, bool(r["confidentiel"]))
    dossier = cfg.dossier_archive.joinpath(*[nom_sur(x) for x in parties])
    cible = ancien
    if ancien.parent != dossier:
        dossier.mkdir(parents=True, exist_ok=True)
        cible = chemin_libre(dossier, *_nom_ext(ancien.name))
        os.replace(ancien, cible)
        _nettoyer(ancien.parent, cfg.dossier_archive)
    with conn:
        conn.execute("UPDATE fichiers SET chemin_local = ?, theme = ?, type_doc = ?, annee = ?, type_manuel = MAX(type_manuel, ?) "
                     "WHERE compte = ? AND sha256 = ?", (str(cible), theme, type_doc, annee, 1 if manuel else 0, compte, sha256))
        conn.execute("UPDATE pieces_recuperees SET chemin_local = ?, theme = ? WHERE sha256 = ? AND chemin_local = ?",
                     (str(cible), theme, sha256, str(ancien)))
    return str(cible)


def _nettoyer(dossier: Path, racine: Path) -> None:
    """Supprime les dossiers devenus vides en remontant (sans jamais toucher à la racine de l'archive)."""
    try:
        racine = racine.resolve()
        d = dossier.resolve()
        while d != racine and racine in d.parents and not any(d.iterdir()):
            d.rmdir()
            d = d.parent
    except OSError:
        pass


# ───────────────────────── rapport ─────────────────────────

def ecrire_rapport(cfg: Config, conn, bilan: Bilan, scan) -> Path:
    dossier = cfg.dossier_rapports / f"recuperation_{bilan.debut:%Y-%m-%d_%H%M%S}"
    n = 2
    while dossier.exists():  # jamais écraser un rapport existant
        dossier = dossier.with_name(f"recuperation_{bilan.debut:%Y-%m-%d_%H%M%S}_{n}")
        n += 1
    dossier.mkdir(parents=True, exist_ok=True)

    with open(dossier / "fichiers_classes.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["compte", "theme", "date", "correspondant", "objet", "fichier d'origine", "fichier classé", "doublon"])
        for r in conn.execute("SELECT * FROM pieces_recuperees ORDER BY compte, theme, date_mail"):
            w.writerow([r["compte"], r["theme"], r["date_mail"], r["correspondant"], r["objet"], r["nom_origine"],
                        r["chemin_local"], "oui" if r["doublon"] else ""])
    with open(dossier / "nouveaux_fichiers.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["compte", "theme", "date", "correspondant", "objet", "origine", "fichier", "indices", "confidentiel"], delimiter=";")
        w.writeheader()
        w.writerows(bilan.nouveaux)

    total = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(taille), 0) o FROM fichiers").fetchone()
    themes_total = conn.execute(
        "SELECT compte, theme, COUNT(*) n, SUM(taille) o FROM fichiers GROUP BY compte, theme ORDER BY compte, o DESC"
    ).fetchall()
    mails_total = conn.execute("SELECT COUNT(*) n FROM mails_recuperes WHERE fichier_eml IS NOT NULL").fetchone()["n"]
    a_classer_total = sum(r["n"] for r in themes_total if r["theme"] in (A_CLASSER, A_CLASSER_CONF))
    conf_total = conn.execute("SELECT COUNT(*) FROM fichiers WHERE confidentiel = 1").fetchone()[0]

    par_theme = defaultdict(lambda: [0, 0])
    for r in themes_total:
        par_theme[r["theme"].split("/")[0]][0] += r["o"] or 0
        par_theme[r["theme"].split("/")[0]][1] += r["n"]

    alertes = list(bilan.erreurs[:25])
    if len(bilan.erreurs) > 25:
        alertes.append(f"… et {len(bilan.erreurs) - 25} autres incidents (voir rapport.txt).")
    if bilan.mails_disparus:
        alertes.append(f"{pluriel(bilan.mails_disparus, 'mail')} inventorié(s) par le scan n'existe(nt) plus sur le serveur (supprimé(s) entre-temps).")

    html = page(
        "Rapport de récupération et de classement",
        f"Réalisé le {bilan.debut:%d/%m/%Y à %H:%M}, d'après le scan à blanc du "
        f"{datetime.fromisoformat(scan['date']):%d/%m/%Y à %H:%M}. Aucun mail n'a été modifié ni supprimé sur les serveurs.",
        [
            Tuile(nombre(bilan.pj_classees), "pièces jointes classées lors de cette récupération", taille_lisible(bilan.octets_pj)),
            Tuile(nombre(bilan.mails_sauvegardes), "mails sauvegardés (.eml)", taille_lisible(bilan.octets_mails)),
            Tuile(nombre(bilan.doublons), "doublons évités", "même fichier déjà présent dans l'archive"),
            Tuile(nombre(a_classer_total), "fichiers « À classer »", "aucune règle reconnue (voir regles_classement.json)"),
            Tuile(nombre(conf_total), "fichiers confidentiels", "gendarmerie / diplomatie : jamais transmis au tri assisté"),
        ],
        [
            Barres("Archive complète par thème", [(k, v[0], v[1]) for k, v in sorted(par_theme.items(), key=lambda kv: -kv[1][0])],
                   note=f"Toute l'archive : {pluriel(total['n'], 'fichier')}, {taille_lisible(total['o'])}, et {pluriel(mails_total, 'mail sauvegardé', 'mails sauvegardés')}."),
            Tableau("Détail par compte et sous-thème", ["Compte", "Thème", "Fichiers", "Volume"],
                    [[r["compte"], r["theme"], nombre(r["n"]), taille_lisible(r["o"] or 0)] for r in themes_total], numeriques={2, 3}),
            Tableau("Aperçu des fichiers classés lors de cette récupération (100 premiers)",
                    ["Date", "Compte", "Thème", "Fichier d'origine", "Correspondant", "Indices"],
                    [[n["date"], n["compte"], n["theme"], n["origine"], n["correspondant"], n["indices"]] for n in bilan.nouveaux[:100]],
                    note="Liste complète : nouveaux_fichiers.csv (s'ouvre dans Excel)."),
        ],
        alertes,
    )
    (dossier / "rapport.html").write_text(html, encoding="utf-8")

    L = [
        "RAPPORT DE RÉCUPÉRATION ET DE CLASSEMENT",
        f"Réalisé le {bilan.debut:%d/%m/%Y à %H:%M} — aucun mail modifié ni supprimé sur les serveurs.",
        "",
        f"  Pièces jointes classées : {nombre(bilan.pj_classees)} ({taille_lisible(bilan.octets_pj)})",
        f"  Mails sauvegardés (.eml) : {nombre(bilan.mails_sauvegardes)} ({taille_lisible(bilan.octets_mails)})",
        f"  Doublons évités          : {nombre(bilan.doublons)}",
        f"  Déjà récupérés avant     : {nombre(bilan.deja_faits)} mails",
        f"  À classer (non reconnus) : {nombre(bilan.a_classer)}",
        f"  Confidentiels (gendarmerie, diplomatie — tri local uniquement) : {nombre(bilan.confidentiels)}",
        "",
        "── PAR THÈME (cette récupération) ──",
        *[f"  {t:<48} {n:>6} fichiers  {taille_lisible(bilan.octets_theme[t]):>10}" for t, n in bilan.par_theme.most_common()],
    ]
    if bilan.erreurs:
        L += ["", "── INCIDENTS ──", *[f"  {e}" for e in bilan.erreurs]]
    (dossier / "rapport.txt").write_text("\n".join(L) + "\n", encoding="utf-8-sig")
    progression.r().texte("\n" + "\n".join(L))
    return dossier / "rapport.html"


DERNIER_BILAN: Bilan | None = None


def reste_a_faire(cfg: Config, sauvegarder: bool, depuis: str | None = None) -> dict:
    """Ce que la récupération téléchargerait encore (d'après le dernier scan et ce qui est déjà fait)."""
    conn = db.ouvrir(cfg.base_index)
    scan = db.dernier_scan_complet(conn)
    res = {"mails": 0, "octets_mails": 0, "pj": 0, "octets_pj": 0, "dossiers": []}
    if scan is None:
        conn.close()
        return res
    for d in conn.execute("SELECT * FROM dossiers WHERE scan_id = ?", (scan["id"],)).fetchall():
        if not dossier_retenu(cfg.recuperation, d["role"], d["nom"]):
            continue
        res["dossiers"].append(f"{d['compte']} / {d['nom']}")
        traites = {r["uid"]: bool(r["fichier_eml"]) for r in conn.execute(
            "SELECT uid, fichier_eml FROM mails_recuperes WHERE compte = ? AND dossier = ? AND uidvalidity = ?",
            (d["compte"], d["brut"], d["uidvalidity"]))}
        params = [scan["id"], d["compte"], d["brut"]]
        filtre = ""
        if depuis:
            filtre, params = " AND date >= ?", params + [depuis]
        for m in conn.execute(f"SELECT uid, taille, nb_pj FROM messages WHERE scan_id = ? AND compte = ? AND dossier = ?{filtre}", params):
            if m["uid"] not in traites and m["nb_pj"]:
                pj = conn.execute("SELECT COUNT(*), COALESCE(SUM(taille), 0) FROM pieces WHERE scan_id = ? AND compte = ? "
                                  "AND dossier = ? AND uid = ? AND statut = 'pièce jointe'",
                                  (scan["id"], d["compte"], d["brut"], m["uid"])).fetchone()
                res["pj"] += pj[0]
                res["octets_pj"] += pj[1]
            if sauvegarder and not traites.get(m["uid"]):
                res["mails"] += 1
                res["octets_mails"] += m["taille"] or 0
    conn.close()
    return res


def lancer(cfg: Config, ouvrir: bool = True, comptes_filtre: list[str] | None = None,
           sauvegarder: bool | None = None, depuis: str | None = None) -> Path | None:
    """sauvegarder : True = mail complet + pièces jointes, False = pièces jointes uniquement (None = réglage)."""
    conn = db.ouvrir(cfg.base_index)
    scan = db.dernier_scan_complet(conn)
    if scan is None:
        progression.r().alerte("Aucun scan à blanc complet n'a été trouvé : lancez d'abord le scan (étape 2), il est obligatoire.")
        return None
    classeur = Classeur.charger(exclusions=cfg.raw.get("exclusions"))
    bilan = Bilan(debut=datetime.now())
    progression.r().texte(f"Récupération basée sur le scan du {datetime.fromisoformat(scan['date']):%d/%m/%Y à %H:%M}.")
    progression.r().texte("Les mails restent intacts sur les serveurs (lecture seule).")
    rec = Recuperateur(cfg, conn, scan["id"], classeur, bilan, sauvegarder=sauvegarder, depuis=depuis)
    wanted = {x.lower() for x in comptes_filtre or []}
    for compte in cfg.comptes:
        if not wanted or compte.nom.lower() in wanted:
            rec.compte(compte)
    chemin = ecrire_rapport(cfg, conn, bilan, scan)
    conn.close()
    global DERNIER_BILAN
    DERNIER_BILAN = bilan
    progression.r().texte(f"\nRapport complet : {chemin}")
    progression.r().texte(f"Vos fichiers : {cfg.dossier_archive}")
    if ouvrir:
        ouvrir_fichier(chemin)
    return chemin
