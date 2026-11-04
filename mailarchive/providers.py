"""Reconnaissance de la messagerie à partir de l'adresse e-mail."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fournisseur:
    type: str
    nom: str  # nom du dossier dans l'archive (Apple, Free…)
    hote: str
    port: int = 993
    aide_mdp: str = ""
    supporte: bool = True


ICLOUD = Fournisseur(
    "icloud", "Apple", "imap.mail.me.com",
    aide_mdp=(
        "iCloud exige un « mot de passe pour app » (PAS votre mot de passe Apple habituel).\n"
        "  Créez-le sur https://account.apple.com > Connexion et sécurité > Mots de passe pour app."
    ),
)
FREE = Fournisseur("free", "Free", "imap.free.fr", aide_mdp="Mot de passe de votre boîte mail Free (celui du webmail).")

_DOMAINES = {
    ("icloud.com", "me.com", "mac.com"): ICLOUD,
    ("free.fr",): FREE,
    ("gmail.com", "googlemail.com"): Fournisseur(
        "gmail", "Gmail", "imap.gmail.com",
        aide_mdp="Gmail exige un « mot de passe d'application » : https://myaccount.google.com/apppasswords",
    ),
    ("orange.fr", "wanadoo.fr"): Fournisseur("orange", "Orange", "imap.orange.fr", aide_mdp="Mot de passe de votre messagerie Orange."),
    ("laposte.net",): Fournisseur("laposte", "La Poste", "imap.laposte.net", aide_mdp="Mot de passe de votre messagerie La Poste."),
    ("sfr.fr", "neuf.fr"): Fournisseur("sfr", "SFR", "imap.sfr.fr", aide_mdp="Mot de passe de votre messagerie SFR."),
    ("yahoo.fr", "yahoo.com"): Fournisseur(
        "yahoo", "Yahoo", "imap.mail.yahoo.com",
        aide_mdp="Yahoo exige un « mot de passe d'application » (Sécurité du compte > Générer un mot de passe d'application).",
    ),
    ("gmx.fr", "gmx.com", "gmx.net"): Fournisseur("gmx", "GMX", "imap.gmx.com", aide_mdp="Mot de passe de votre compte GMX."),
    ("outlook.com", "outlook.fr", "hotmail.com", "hotmail.fr", "live.com", "live.fr", "msn.com"): Fournisseur(
        "microsoft", "Outlook", "outlook.office365.com", supporte=False,
        aide_mdp="Microsoft n'autorise plus la connexion IMAP par mot de passe pour ces adresses (pas encore géré).",
    ),
}


def detecter(email: str) -> Fournisseur | None:
    domaine = email.rsplit("@", 1)[-1].strip().lower()
    for domaines, f in _DOMAINES.items():
        if domaine in domaines:
            return f
    return None


def par_type(type_: str) -> Fournisseur | None:
    for f in _DOMAINES.values():
        if f.type == type_:
            return f
    return None
