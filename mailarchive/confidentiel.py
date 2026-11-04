"""Documents confidentiels (gendarmerie, diplomatie) : JAMAIS transmis à un service externe.

Règle impérative : tout document lié à ces domaines est trié uniquement en
local par l'outil. Le filtre est appliqué sur l'ordinateur, avant tout export
pour le tri assisté. Il est volontairement large : en cas de doute, le document
est considéré comme confidentiel. config.json peut AJOUTER des critères
(section "confidentiel", ou menu 4 → 4 « Mots à exclure du tri »), jamais en retirer.
"""
from __future__ import annotations

from .themes import normaliser

DOMAINES = [
    # Gendarmerie / ministère de l'Intérieur / Défense
    "gendarmerie.interieur.gouv.fr", "interieur.gouv.fr", "gendarmerie.fr", "intradef.gouv.fr",
    "def.gouv.fr", "defense.gouv.fr",
    # Diplomatie / ministère de l'Europe et des Affaires étrangères
    "diplomatie.gouv.fr", "france-diplomatie.gouv.fr",
]
# Un domaine contenant l'un de ces fragments est confidentiel (ex. ambafrance-us.org, consulfrance-xxx.org).
FRAGMENTS_DOMAINE = ["gendarmerie", "diplomatie", "ambafrance", "consulfrance", "intradef"]
MOTS = [
    # Gendarmerie
    "gendarmerie", "gendarme", "gendarmes", "dggn", "ggd", "egm", "escadron", "brigade de gendarmerie",
    "compagnie de gendarmerie", "region de gendarmerie", "groupement de gendarmerie", "sous officier de gendarmerie",
    "officier de gendarmerie", "ministere de l interieur", "ministere des armees", "intradef",
    # Gendarmerie : vocabulaire de métier (documents qui ne citent pas le mot « gendarmerie »)
    "eg", "egm", "pgm", "psig", "cob", "bta", "brigade", "peloton", "escadrons", "maintien de l ordre",
    "gendarmes mobiles", "garde republicaine",
    "gilet pare balles", "gilets pare balles", "pare balles", "bouclier", "boucliers",
    "personnels", "militaire", "militaires",
    "gd", "adc", "adj", "mdc", "mdl", "mjr", "ltn", "cne", "cdt", "lcl", "sous officier", "sous officiers",
    "opj", "apj",
    "ordre de mission", "note de service", "service courant", "permanence", "astreinte",
    # Diplomatie
    "diplomatie", "diplomatique", "diplomatiques", "diplomate", "ambassade", "ambassadeur", "consulat",
    "consulaire", "affaires etrangeres", "quai d orsay", "meae", "passeport de service",
    "passeport diplomatique", "valise diplomatique", "attache de securite interieure",
]

# Messageries de particuliers. Un document échangé UNIQUEMENT entre particuliers (vous compris)
# n'est jamais transmis au tri assisté : c'est par là que passent les documents de travail
# envoyés depuis des adresses personnelles (règle de prudence).
MESSAGERIES_PERSONNELLES = [
    "gmail.com", "googlemail.com", "icloud.com", "me.com", "mac.com", "free.fr", "laposte.net",
    "orange.fr", "wanadoo.fr", "sfr.fr", "neuf.fr", "bbox.fr", "numericable.fr", "hotmail.com",
    "hotmail.fr", "outlook.com", "outlook.fr", "live.com", "live.fr", "msn.com", "yahoo.com", "yahoo.fr",
    "gmx.com", "gmx.fr", "gmx.net", "aol.com", "aol.fr", "protonmail.com", "proton.me", "club-internet.fr",
    "aliceadsl.fr", "cegetel.net", "voila.fr", "tutanota.com",
]


def echange_entre_particuliers(adresses: list[str]) -> bool:
    """Vrai si toutes les adresses du mail sont des messageries de particuliers (ou si aucune n'est connue)."""
    domaines = [a.lower().rsplit("@", 1)[-1] for a in adresses if "@" in a]
    return not domaines or all(d in MESSAGERIES_PERSONNELLES for d in domaines)


class Filtre:
    def __init__(self, ajouts: dict | None = None):
        ajouts = ajouts or {}
        self.domaines = [d.lower().strip() for d in DOMAINES + list(ajouts.get("domaines", [])) if d.strip()]
        self.fragments = [f.lower().strip() for f in FRAGMENTS_DOMAINE + list(ajouts.get("fragments_domaine", [])) if f.strip()]
        self.mots = [normaliser(m) for m in MOTS + list(ajouts.get("mots", [])) if m.strip()]

    def adresse(self, adresse: str) -> str | None:
        domaine = (adresse or "").lower().rsplit("@", 1)[-1].strip()
        if not domaine:
            return None
        for d in self.domaines:
            if domaine == d or domaine.endswith("." + d):
                return f"adresse {d}"
        for f in self.fragments:
            if f in domaine:
                return f"adresse contenant « {f} »"
        return None

    def texte(self, *textes: str) -> str | None:
        for t in textes:
            if not t:
                continue
            n = normaliser(t)
            for m in self.mots:
                if m in n:
                    return f"mot « {m.strip()} »"
        return None

    def verdict(self, adresses: list[str], *textes: str) -> str | None:
        """Raison pour laquelle c'est confidentiel, ou None."""
        for a in adresses:
            r = self.adresse(a)
            if r:
                return r
        return self.texte(*textes)
