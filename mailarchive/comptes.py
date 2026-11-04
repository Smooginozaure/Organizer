"""Ajout d'une adresse mail : détection de la messagerie, test de connexion, enregistrement."""
from __future__ import annotations

import getpass

from . import config as cfgmod
from . import credentials, providers
from .config import Compte
from .imap_client import AuthError, Session


def _nom_libre(raw: dict, nom: str, email: str) -> str:
    pris = {c["nom"].lower(): c.get("email", "").lower() for c in raw.get("comptes", [])}
    if nom.lower() not in pris or pris[nom.lower()] == email.lower():
        return nom
    return f"{nom} ({email.split('@')[0]})"


def tester(compte: Compte, password: str) -> tuple[bool, str, str | None]:
    """(succès, message, identifiant qui a fonctionné)."""
    session = Session(compte, password, timeout=30)
    try:
        session.connect()
        nb = len([d for d in session.dossiers() if d.selectionnable])
        return True, f"Connexion réussie ({nb} dossiers trouvés).", session.login_utilise
    except AuthError as e:
        return False, f"Connexion refusée : {e}", None
    except Exception as e:
        return False, f"Impossible de joindre {compte.hote} : {e}", None
    finally:
        session.close()


def enregistrer(email: str, password: str | None = None, hote: str | None = None, essais: int = 3) -> bool:
    """Ajoute (ou met à jour) une adresse. Demande le mot de passe si non fourni."""
    email = email.strip()
    if "@" not in email:
        print("✗ Adresse e-mail invalide.")
        return False
    raw = cfgmod.load_raw()
    raw.setdefault("comptes", [])
    existant = next((c for c in raw["comptes"] if c.get("email", "").lower() == email.lower()), None)

    f = providers.detecter(email)
    if existant:
        compte = cfgmod.compte_depuis_raw(existant)
        print(f"Adresse déjà connue (compte « {compte.nom} ») : mise à jour du mot de passe.")
    else:
        if f is not None and not f.supporte:
            print(f"✗ {f.aide_mdp}")
            return False
        if f is None:
            domaine = email.split("@")[1]
            print(f"Messagerie non reconnue pour « {domaine} ».")
            hote = hote or input(f"  Serveur IMAP (par ex. imap.{domaine}) : ").strip()
            if not hote:
                return False
            f = providers.Fournisseur("autre", domaine.split(".")[0].capitalize(), hote)
        compte = Compte(nom=_nom_libre(raw, f.nom, email), type=f.type, email=email, hote=hote or f.hote, port=f.port)
        print(f"Messagerie détectée : {f.nom} ({compte.hote}). Ce compte sera rangé dans « Archive mail\\{compte.nom} ».")

    aide = (f.aide_mdp if f else "")
    for essai in range(1, essais + 1):
        if password is None:
            if aide:
                print(aide)
            pwd = getpass.getpass(f"Mot de passe pour {email} (la saisie reste invisible) : ").strip()
        else:
            pwd = password.strip()
        if not pwd:
            print("Mot de passe vide : abandon.")
            return False
        print(f"Connexion à {compte.hote}…")
        ok, message, login = tester(compte, pwd)
        print(("✓ " if ok else "✗ ") + message)
        if ok:
            credentials.set_password(compte, pwd)
            entree = existant or {"nom": compte.nom, "type": compte.type, "email": email}
            if compte.type == "autre" or (f and compte.hote != f.hote):
                entree["hote"] = compte.hote
            if login and login != email:
                entree["identifiant"] = login
            if not existant:
                raw["comptes"].append(entree)
            cfgmod.save_raw(raw)
            print("  Mot de passe enregistré dans le Gestionnaire d'identifiants Windows.")
            return True
        if password is not None:
            return False
        if essai < essais:
            print(f"  Nouvel essai ({essai + 1}/{essais})…")
    print("  Le compte n'a PAS été enregistré.")
    return False


def supprimer(email: str) -> bool:
    raw = cfgmod.load_raw()
    entree = next((c for c in raw.get("comptes", []) if c.get("email", "").lower() == email.lower()), None)
    if not entree:
        return False
    credentials.delete_password(cfgmod.compte_depuis_raw(entree))
    raw["comptes"].remove(entree)
    cfgmod.save_raw(raw)
    return True
