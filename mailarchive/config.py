"""Lecture / écriture de config.json (comptes, dossier d'archive, options de récupération)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import providers

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

RECUPERATION_DEFAUT = {
    # Rôles de dossiers récupérés. Rôles possibles : réception, envoyés, brouillons, autre, corbeille, spam.
    "roles": ["réception", "envoyés", "brouillons"],
    # Noms de dossiers à ajouter ou à retirer en plus des rôles ci-dessus.
    "dossiers_inclus": [],
    "dossiers_exclus": [],
    # Sauvegarder aussi chaque mail complet (.eml) en plus des pièces jointes classées.
    "sauvegarder_mails": True,
}


def dossier_retenu(recup: dict, role: str, nom: str) -> bool:
    """Un dossier de la boîte est-il inclus dans la récupération ?"""
    low = nom.lower()
    if low in {x.lower() for x in recup.get("dossiers_exclus") or []}:
        return False
    if low in {x.lower() for x in recup.get("dossiers_inclus") or []}:
        return True
    return role in (recup.get("roles") or [])


def desktop_dir() -> Path:
    """Bureau réel de l'utilisateur (peut être redirigé, par ex. vers OneDrive)."""
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, _ = winreg.QueryValueEx(k, "Desktop")
            return Path(os.path.expandvars(value))
    except Exception:
        return Path.home() / "Desktop"


@dataclass
class Compte:
    nom: str
    type: str
    email: str
    hote: str
    port: int
    identifiant: str = ""  # vide = essai automatique (adresse complète puis partie avant @)

    def logins(self) -> list[str]:
        if self.identifiant:
            return [self.identifiant]
        out = [self.email]
        local = self.email.split("@")[0]
        if local and local != self.email:
            out.append(local)
        return out


@dataclass
class Config:
    dossier_archive: Path
    comptes: list[Compte] = field(default_factory=list)
    recuperation: dict = field(default_factory=lambda: dict(RECUPERATION_DEFAUT))
    raw: dict = field(default_factory=dict)

    @property
    def dossier_rapports(self) -> Path:
        return self.dossier_archive / "_rapports"

    @property
    def base_index(self) -> Path:
        return self.dossier_archive / "_index" / "archive.sqlite"


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(path.replace("{BUREAU}", str(desktop_dir())))).expanduser()


def load_raw() -> dict:
    src = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    return json.loads(src.read_text(encoding="utf-8"))


def save_raw(raw: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compte_depuis_raw(c: dict) -> Compte:
    f = providers.par_type(c.get("type", "").lower())
    return Compte(
        nom=c["nom"],
        type=c.get("type", ""),
        email=(c.get("email") or "").strip(),
        hote=c.get("hote") or (f.hote if f else ""),
        port=int(c.get("port") or (f.port if f else 993)),
        identifiant=c.get("identifiant", ""),
    )


def load_config() -> Config:
    raw = load_raw()
    comptes = [compte_depuis_raw(c) for c in raw.get("comptes", []) if (c.get("email") or "").strip()]
    recup = dict(RECUPERATION_DEFAUT)
    recup.update(raw.get("recuperation") or {})
    return Config(
        dossier_archive=_expand(raw.get("dossier_archive", "{BUREAU}/Archive mail")),
        comptes=comptes,
        recuperation=recup,
        raw=raw,
    )
