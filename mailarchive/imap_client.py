"""Connexion IMAP STRICTEMENT en lecture seule, avec reconnexion automatique.

Double protection contre toute modification des boîtes mail :
1. les dossiers sont ouverts avec EXAMINE (lecture seule côté serveur) ;
2. la classe ImapLectureSeule refuse, avant tout envoi au serveur, chaque
   commande capable de modifier, déplacer, supprimer ou marquer un mail.
"""
from __future__ import annotations

import imaplib
import re
import socket
import ssl
import time
from dataclasses import dataclass

from .config import Compte
from .imap_parse import decode_imap_utf7, parse_list

imaplib._MAXLINE = 50_000_000  # certaines lignes BODYSTRUCTURE sont très longues

TRASH_NAMES = ("trash", "deleted", "corbeille", "supprim", "poubelle")
JUNK_NAMES = ("junk", "spam", "indésirable", "indesirable", "pourriel", "bulk")
SENT_NAMES = ("sent", "envoy")
DRAFT_NAMES = ("draft", "brouillon")

# Seules ces commandes peuvent atteindre le serveur.
COMMANDES_AUTORISEES = {
    "CAPABILITY", "NOOP", "LOGIN", "AUTHENTICATE", "LOGOUT", "LIST", "LSUB",
    "STATUS", "EXAMINE", "SEARCH", "FETCH", "UID", "ID", "NAMESPACE",
}
SOUS_COMMANDES_UID_AUTORISEES = {"SEARCH", "FETCH"}
# FETCH de BODY[...] ou RFC822 (sans .PEEK) marquerait le mail comme lu : interdit.
_FETCH_INTERDIT = re.compile(r"(?<![.\w])(BODY\[|RFC822(?![.\w]))", re.IGNORECASE)


class OperationInterdite(PermissionError):
    pass


class AuthError(Exception):
    pass


def verifier_commande(name: str, args: tuple) -> None:
    nom = name.upper()
    if nom not in COMMANDES_AUTORISEES:
        raise OperationInterdite(f"Commande IMAP « {nom} » refusée : l'outil travaille en lecture seule.")
    if nom == "UID":
        sous = str(args[0]).upper() if args else ""
        if sous not in SOUS_COMMANDES_UID_AUTORISEES:
            raise OperationInterdite(f"Commande IMAP « UID {sous} » refusée : l'outil travaille en lecture seule.")
        if sous == "FETCH":
            items = " ".join(str(a) for a in args[2:])
            if _FETCH_INTERDIT.search(items):
                raise OperationInterdite(f"Lecture sans BODY.PEEK refusée (marquerait les mails comme lus) : {items}")
    if nom == "FETCH":
        items = " ".join(str(a) for a in args[1:])
        if _FETCH_INTERDIT.search(items):
            raise OperationInterdite(f"Lecture sans BODY.PEEK refusée (marquerait les mails comme lus) : {items}")


class ImapLectureSeule(imaplib.IMAP4_SSL):
    def _command(self, name, *args):
        verifier_commande(name, args)
        return super()._command(name, *args)


@dataclass
class Dossier:
    brut: str  # nom tel que le serveur l'attend
    nom: str  # nom lisible
    drapeaux: list[str]

    @property
    def selectionnable(self) -> bool:
        f = {x.lower() for x in self.drapeaux}
        return "\\noselect" not in f and "\\nonexistent" not in f

    @property
    def role(self) -> str:
        f = {x.lower() for x in self.drapeaux}
        low = self.nom.lower()
        if "\\all" in f:
            return "tous"
        if "\\trash" in f or any(n in low for n in TRASH_NAMES):
            return "corbeille"
        if "\\junk" in f or any(n in low for n in JUNK_NAMES):
            return "spam"
        if "\\sent" in f or any(n in low for n in SENT_NAMES):
            return "envoyés"
        if "\\drafts" in f or any(n in low for n in DRAFT_NAMES):
            return "brouillons"
        if low == "inbox":
            return "réception"
        return "autre"


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Session:
    def __init__(self, compte: Compte, password: str, timeout: int = 120):
        self.compte = compte
        self.password = password
        self.timeout = timeout
        self.conn: ImapLectureSeule | None = None
        self.login_utilise: str | None = None
        self._dossier: Dossier | None = None
        self.uidvalidity: str = ""

    def connect(self) -> None:
        ctx = ssl.create_default_context()
        candidats = [self.login_utilise] if self.login_utilise else self.compte.logins()
        derniere_erreur = None
        for login in candidats:
            conn = ImapLectureSeule(self.compte.hote, self.compte.port, ssl_context=ctx, timeout=self.timeout)
            try:
                conn.login(login, self.password)
            except imaplib.IMAP4.error as e:
                derniere_erreur = e
                try:
                    conn.logout()
                except Exception:
                    pass
                continue
            self.conn = conn
            self.login_utilise = login
            return
        raise AuthError(f"Connexion refusée par {self.compte.hote} : {derniere_erreur}")

    def close(self) -> None:
        # LOGOUT seulement (jamais CLOSE, qui purge les mails marqués supprimés en mode écriture).
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def _reconnect(self) -> None:
        self.close()
        self.connect()
        if self._dossier is not None:
            self.conn.select(_quote(self._dossier.brut), readonly=True)

    def _call(self, fn, *args, essais: int = 3):
        for tentative in range(essais):
            try:
                typ, data = fn(self.conn)(*args)
                if typ != "OK":
                    raise imaplib.IMAP4.error(f"{typ} {data!r}")
                return data
            except (imaplib.IMAP4.abort, OSError, socket.timeout, ssl.SSLError) as e:
                if isinstance(e, OperationInterdite) or tentative == essais - 1:
                    raise
                time.sleep(3 * (tentative + 1))
                self._reconnect()

    def dossiers(self) -> list[Dossier]:
        data = self._call(lambda c: c.list, '""', '"*"')
        return [Dossier(brut=name, nom=decode_imap_utf7(name), drapeaux=flags) for flags, _d, name in parse_list(data)]

    def ouvrir(self, dossier: Dossier) -> int:
        """Ouvre un dossier en LECTURE SEULE (EXAMINE) : aucun mail n'est modifié ni marqué lu."""
        self._dossier = dossier
        data = self._call(lambda c: c.select, _quote(dossier.brut), True)
        _typ, val = self.conn.response("UIDVALIDITY")
        self.uidvalidity = val[0].decode() if val and val[0] else ""
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def uids(self) -> list[str]:
        data = self._call(lambda c: c.uid, "SEARCH", None, "ALL")
        return data[0].decode().split() if data and data[0] else []

    def fetch(self, uids: list[str], items: str):
        return self._call(lambda c: c.uid, "FETCH", ",".join(uids), items)
