"""Mots de passe stockés dans le Gestionnaire d'identifiants Windows (jamais en clair sur le disque)."""
from __future__ import annotations

import keyring

from .config import Compte

SERVICE = "ArchiveMail"


def _key(compte: Compte) -> str:
    return f"{compte.nom}:{compte.email}"


def get_password(compte: Compte) -> str | None:
    return keyring.get_password(SERVICE, _key(compte))


def set_password(compte: Compte, password: str) -> None:
    keyring.set_password(SERVICE, _key(compte), password)


def delete_password(compte: Compte) -> None:
    try:
        keyring.delete_password(SERVICE, _key(compte))
    except keyring.errors.PasswordDeleteError:
        pass
