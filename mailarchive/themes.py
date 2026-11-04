"""Classement 100 % local (rien ne quitte l'ordinateur).

Deux décisions indépendantes, prises à partir de mots-clés (regles_classement.json) :
- le TYPE de document (facture, ticket de caisse, contrat…) : premier niveau de l'arborescence ;
- le DOMAINE (énergie, santé, logement…) : niveaux suivants, de plus en plus précis.
Arborescence : <Type>/<Domaine>/<Sous-domaine>/<Année>, ex. Factures/Énergie/Électricité - Gaz/2024.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .classify import QUARANTINE

REGLES_PATH = Path(__file__).resolve().parent.parent / "regles_classement.json"
REGLES_EXEMPLE = REGLES_PATH.with_name("regles_classement.example.json")
if not REGLES_PATH.exists() and REGLES_EXEMPLE.exists():
    # Premier lancement : vos règles partent de l'exemple fourni, puis restent les vôtres (fichier non publié).
    import shutil

    shutil.copyfile(REGLES_EXEMPLE, REGLES_PATH)

# Poids selon l'endroit où le mot-clé est trouvé.
POIDS_NOM_FICHIER = 3.0
POIDS_OBJET = 2.0
POIDS_EXPEDITEUR = 3.0
POIDS_CONTENU = 1.0

A_CLASSER = "À classer"
A_CLASSER_CONF = "À classer - confidentiel"
QUARANTAINE = "_Quarantaine (ne pas ouvrir)"
PHOTOS = "Photos et vidéos"
AGENDA = "Agenda et contacts"
AUTRES = "Autres documents"
TRI_MANUEL = "À trier soi-même"  # documents contenant un mot « à trier soi-même » (aucun tri automatique ni IA)
DIVERS = "Divers"

# Domaine de secours (ancien nom de « thème ») selon la catégorie de fichier.
SECOURS = {
    "Images / photos": PHOTOS,
    "Vidéos": PHOTOS,
    "Audio": "Audio",
    "Agenda / contacts": AGENDA,
    QUARANTINE: QUARANTAINE,
}
# Type de secours selon la catégorie de fichier, quand aucun mot-clé de type n'est reconnu.
TYPE_SECOURS = {
    "Images / photos": PHOTOS,
    "Vidéos": PHOTOS,
    "Audio": "Audio",
    "Agenda / contacts": AGENDA,
    "Tableurs": "Tableaux et listes",
    "Présentations": "Notices, guides et présentations",
    "Mails joints": "Mails transférés",
    "Archives (zip…)": "Archives compressées",
    QUARANTINE: QUARANTAINE,
}
# Types qui se suffisent à eux-mêmes (pas de sous-dossier de domaine si aucun n'est reconnu).
TYPES_SANS_DOMAINE = {PHOTOS, AGENDA, "Audio", QUARANTAINE, "Partitions"}
# Anciens thèmes devenus des domaines (le type est désormais séparé).
ALIAS_DOMAINES = {
    "Achats - Factures": "Achats",
    "Contrats/Autres contrats": "",
    "Travail/Bulletins de salaire": "Travail",
    "Banque - Finances/Relevés et RIB": "Banque - Finances/Comptes",
    "Administratif/Courriers officiels": "Administratif/Organismes",
}
SANS_DOMAINE = {A_CLASSER, A_CLASSER_CONF, PHOTOS, AGENDA, "Audio", QUARANTAINE, ""}


def normaliser(texte: str) -> str:
    """Minuscules, sans accents, ponctuation remplacée par des espaces."""
    t = unicodedata.normalize("NFKD", texte or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = t.replace("œ", "oe").replace("æ", "ae")
    return " " + re.sub(r"[^a-z0-9]+", " ", t).strip() + " "


@dataclass
class Regle:
    theme: str  # domaine (« Énergie/Électricité - Gaz ») ou type (« Factures »)
    mots: list[str]  # normalisés, entourés d'espaces
    expediteurs: list[str]
    poids: float = 1.0


@dataclass
class Decision:
    theme: str
    score: float
    indices: list[str]


def _regles(liste: list[dict], cle: str) -> list[Regle]:
    return [
        Regle(
            theme=r[cle],
            mots=[normaliser(m) for m in r.get("mots", []) if m.strip()],
            expediteurs=[e.lower().strip() for e in r.get("expediteurs", []) if e.strip()],
            poids=float(r.get("poids", 1.0)),
        )
        for r in liste
    ]


def _sources(nom_fichier: str, objet: str, contenu: str):
    return [
        (normaliser(re.sub(r"[_\-.]+", " ", nom_fichier)), POIDS_NOM_FICHIER, "nom"),
        (normaliser(objet), POIDS_OBJET, "objet"),
        (normaliser(contenu[:6000]), POIDS_CONTENU, "contenu"),
    ]


def _retirer(texte: str, mots: list[str]) -> str:
    """Efface les mots (normalisés, entourés d'espaces) du texte normalisé."""
    for m in mots:
        while m in texte:
            texte = texte.replace(m, " ")
    return texte


def mots_utilisateur(liste) -> list[str]:
    """Mots saisis par l'utilisateur, normalisés et dédoublonnés (ordre conservé)."""
    vus: list[str] = []
    for m in liste or []:
        n = normaliser(str(m))
        if n.strip() and n not in vus:
            vus.append(n)
    return vus


def _meilleure(regles: list[Regle], sources, adresse: str) -> Decision | None:
    adresse = (adresse or "").lower()
    meilleur: Decision | None = None
    for r in regles:
        score, indices = 0.0, []
        for mot in r.mots:
            for texte, poids, source in sources:
                if mot in texte:
                    score += poids
                    indices.append(f"{mot.strip()} ({source})")
        for e in r.expediteurs:
            if adresse == e or adresse.endswith("@" + e) or adresse.endswith("." + e):
                score += POIDS_EXPEDITEUR
                indices.append(f"expéditeur {e}")
                break
        score *= r.poids
        if score > 0 and (meilleur is None or score > meilleur.score):
            meilleur = Decision(r.theme, score, indices)
    return meilleur


class Classeur:
    def __init__(self, regles: list[Regle], seuil: float = 2.0, types: list[Regle] | None = None,
                 exclusions: dict | None = None):
        self.regles = regles
        self.types = types or []
        self.seuil = seuil
        exclusions = exclusions or {}
        # Mots ignorés : jamais comptés par les règles (signature, pied de page, mot trompeur…).
        self.ignores = mots_utilisateur(exclusions.get("ignorer"))
        # Mots « à trier soi-même » : le document n'est rangé ni par les règles ni par une IA.
        self.bloquants = mots_utilisateur(exclusions.get("a_trier_soi_meme"))

    @classmethod
    def charger(cls, chemin: Path = REGLES_PATH, exclusions: dict | None = None) -> "Classeur":
        raw = json.loads(chemin.read_text(encoding="utf-8"))
        return cls(_regles(raw.get("regles", []), "theme"), float(raw.get("seuil", 2)), _regles(raw.get("types", []), "type"),
                   exclusions)

    def _sources(self, nom_fichier: str, objet: str, contenu: str):
        return [(_retirer(t, self.ignores), p, s) for t, p, s in _sources(nom_fichier, objet, contenu)]

    def bloquant(self, nom_fichier: str, objet: str, contenu: str) -> str | None:
        """Mot « à trier soi-même » présent dans le nom, l'objet ou le texte, ou None."""
        for t, _, _ in _sources(nom_fichier, objet, contenu):
            for m in self.bloquants:
                if m in t:
                    return m.strip()
        return None

    def touche_ignores(self, nom_fichier: str, objet: str, contenu: str) -> bool:
        return any(m in t for t, _, _ in _sources(nom_fichier, objet, contenu) for m in self.ignores)

    def classer(self, nom_fichier: str, objet: str, adresse: str, contenu: str, categorie: str) -> Decision:
        """Domaine du document."""
        if categorie == QUARANTINE:
            return Decision(QUARANTAINE, 0, ["fichier exécutable"])
        mot = self.bloquant(nom_fichier, objet, contenu)
        if mot:
            return Decision(A_CLASSER, 0, [f"mot à trier soi-même « {mot} »"])
        meilleur = _meilleure(self.regles, self._sources(nom_fichier, objet, contenu), adresse)
        if meilleur and meilleur.score >= self.seuil:
            return meilleur
        return Decision(SECOURS.get(categorie, A_CLASSER), 0, [])

    def classer_type(self, nom_fichier: str, objet: str, contenu: str, categorie: str) -> Decision:
        """Type du document (facture, ticket, contrat…)."""
        if categorie == QUARANTINE:
            return Decision(QUARANTAINE, 0, ["fichier exécutable"])
        mot = self.bloquant(nom_fichier, objet, contenu)
        if mot:
            return Decision(TRI_MANUEL, 0, [f"mot à trier soi-même « {mot} »"])
        meilleur = _meilleure(self.types, self._sources(nom_fichier, objet, contenu), "")
        seuil = self.seuil
        if categorie in ("Images / photos", "Vidéos"):
            seuil = max(seuil, POIDS_NOM_FICHIER)  # une photo n'est un « document » que si son nom l'indique
        if meilleur and meilleur.score >= seuil:
            return meilleur
        return Decision(TYPE_SECOURS.get(categorie, AUTRES), 0, [])


def domaine(theme: str | None) -> list[str]:
    """Niveaux de domaine (de plus en plus précis) à partir du thème enregistré."""
    t = ALIAS_DOMAINES.get(theme or "", theme or "")
    if t in SANS_DOMAINE:
        return []
    return [x for x in t.split("/") if x.strip()]


def arborescence(type_doc: str, theme: str | None, annee: str, confidentiel: bool = False) -> list[str]:
    """Dossiers successifs : <Type>/<Domaine>/<Sous-domaine>/<Année>."""
    if type_doc == QUARANTAINE or theme == QUARANTAINE:
        return [QUARANTAINE, annee]
    if type_doc == TRI_MANUEL:
        return [TRI_MANUEL, annee]
    dom = domaine(theme)
    if type_doc == AUTRES and not dom:
        return [A_CLASSER_CONF if confidentiel else A_CLASSER, annee]
    if type_doc in TYPES_SANS_DOMAINE:
        return [type_doc, *dom, annee]
    return [type_doc, *(dom or [DIVERS]), annee]
