"""Index local (SQLite) : inventaire des scans et suivi de ce qui a déjà été récupéré."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    partiel INTEGER NOT NULL DEFAULT 0,
    termine INTEGER NOT NULL DEFAULT 0,
    rapport TEXT
);
CREATE TABLE IF NOT EXISTS dossiers (
    scan_id INTEGER NOT NULL,
    compte TEXT NOT NULL,
    brut TEXT NOT NULL,
    nom TEXT NOT NULL,
    role TEXT NOT NULL,
    uidvalidity TEXT,
    messages INTEGER,
    PRIMARY KEY (scan_id, compte, brut)
);
CREATE TABLE IF NOT EXISTS messages (
    scan_id INTEGER NOT NULL,
    compte TEXT NOT NULL,
    dossier TEXT NOT NULL,
    uid INTEGER NOT NULL,
    taille INTEGER,
    date TEXT,
    adresse TEXT,
    objet TEXT,
    nb_pj INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scan_id, compte, dossier, uid)
);
CREATE TABLE IF NOT EXISTS pieces (
    scan_id INTEGER NOT NULL,
    compte TEXT NOT NULL,
    dossier TEXT NOT NULL,
    uid INTEGER NOT NULL,
    chemin TEXT NOT NULL,
    nom TEXT,
    categorie TEXT,
    taille INTEGER,
    statut TEXT,
    PRIMARY KEY (scan_id, compte, dossier, uid, chemin)
);
-- Un mail traité (sauvegardé et/ou pièces jointes extraites) n'est plus jamais retéléchargé.
CREATE TABLE IF NOT EXISTS mails_recuperes (
    compte TEXT NOT NULL,
    dossier TEXT NOT NULL,
    uidvalidity TEXT NOT NULL,
    uid INTEGER NOT NULL,
    fichier_eml TEXT,
    date_recuperation TEXT NOT NULL,
    PRIMARY KEY (compte, dossier, uidvalidity, uid)
);
-- Chaque fichier distinct (empreinte SHA-256) n'est stocké qu'une fois par compte.
CREATE TABLE IF NOT EXISTS fichiers (
    compte TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    chemin_local TEXT NOT NULL,
    theme TEXT,
    taille INTEGER,
    PRIMARY KEY (compte, sha256)
);
CREATE TABLE IF NOT EXISTS pieces_recuperees (
    compte TEXT NOT NULL,
    dossier TEXT NOT NULL,
    uidvalidity TEXT NOT NULL,
    uid INTEGER NOT NULL,
    chemin TEXT NOT NULL,
    nom_origine TEXT,
    sha256 TEXT,
    chemin_local TEXT,
    theme TEXT,
    doublon INTEGER NOT NULL DEFAULT 0,
    date_mail TEXT,
    correspondant TEXT,
    objet TEXT,
    PRIMARY KEY (compte, dossier, uidvalidity, uid, chemin)
);
"""


# Colonnes ajoutées après la première version (migration des index existants).
COLONNES_AJOUTEES = [
    ("fichiers", "confidentiel", "INTEGER NOT NULL DEFAULT 0"),
    ("fichiers", "raison_confidentiel", "TEXT"),
    ("pieces_recuperees", "confidentiel", "INTEGER NOT NULL DEFAULT 0"),
    ("fichiers", "type_doc", "TEXT"),
    ("fichiers", "annee", "TEXT"),
    ("fichiers", "type_manuel", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "expediteur", "TEXT"),
    ("pieces", "mime", "TEXT"),
    ("pieces", "taille_encodee", "INTEGER"),
    ("scans", "mode", "TEXT NOT NULL DEFAULT 'complet'"),
    ("scans", "base_id", "INTEGER"),
    ("scans", "nouveaux", "INTEGER"),
    ("scans", "supprimes", "INTEGER"),
]


def ouvrir(chemin: Path) -> sqlite3.Connection:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(chemin)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, colonne, decl in COLONNES_AJOUTEES:
        existantes = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if colonne not in existantes:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {decl}")
    conn.commit()
    return conn


def dernier_scan_integral(conn: sqlite3.Connection):
    """Dernier scan qui a relu TOUS les mails (et non seulement les nouveaux)."""
    return conn.execute(
        "SELECT * FROM scans WHERE termine = 1 AND partiel = 0 AND mode = 'complet' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def dernier_scan_complet(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM scans WHERE termine = 1 AND partiel = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
