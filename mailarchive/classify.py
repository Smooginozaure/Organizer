"""Repérage des vraies pièces jointes et catégorisation par type de fichier."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from .imap_parse import iter_leaf_parts, leaf_info

PJ = "pièce jointe"
DECO = "image décorative"

# Parties techniques, jamais considérées comme des pièces jointes.
IGNORED_MIME = {
    "application/pkcs7-signature",
    "application/x-pkcs7-signature",
    "application/pgp-signature",
    "application/pgp-encrypted",
    "application/pgp-keys",
    "message/delivery-status",
    "message/disposition-notification",
    "message/feedback-report",
    "text/rfc822-headers",
}
IGNORED_EXT = {"p7s", "asc", "sig"}

QUARANTINE = "Exécutables / scripts (quarantaine)"

CATEGORIES: list[tuple[str, set[str], tuple[str, ...]]] = [
    ("PDF", {"pdf"}, ("application/pdf",)),
    ("Images / photos", {"jpg", "jpeg", "png", "gif", "heic", "heif", "bmp", "tif", "tiff", "webp", "svg"}, ("image/",)),
    ("Documents texte", {"doc", "docx", "odt", "rtf", "txt", "pages"}, ("application/msword", "application/vnd.openxmlformats-officedocument.wordprocessing")),
    ("Tableurs", {"xls", "xlsx", "xlsm", "ods", "csv", "numbers"}, ("application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheet")),
    ("Présentations", {"ppt", "pptx", "odp", "key"}, ("application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentation")),
    ("Archives (zip…)", {"zip", "rar", "7z", "gz", "tar", "tgz", "bz2"}, ("application/zip", "application/x-zip", "application/x-rar", "application/x-7z")),
    ("Vidéos", {"mp4", "mov", "avi", "mkv", "3gp", "m4v", "wmv", "webm"}, ("video/",)),
    ("Audio", {"mp3", "m4a", "wav", "aac", "ogg", "amr", "opus", "flac"}, ("audio/",)),
    ("Mails joints", {"eml", "msg"}, ("message/rfc822", "message/global")),
    ("Agenda / contacts", {"ics", "vcf", "vcs"}, ("text/calendar", "text/vcard", "text/x-vcard")),
    (QUARANTINE, {"exe", "msi", "bat", "cmd", "js", "vbs", "vbe", "scr", "ps1", "jar", "com", "lnk", "hta", "wsf", "iso", "img", "apk", "dmg", "pif", "reg"}, ("application/x-msdownload", "application/x-msdos-program")),
]
OTHER = "Autres"


def extension(filename: str) -> str:
    suffix = PurePath(filename).suffix if filename else ""
    return suffix[1:].lower() if suffix else ""


def category(ext: str, mime: str) -> str:
    if ext:
        for name, exts, _ in CATEGORIES:
            if ext in exts:
                return name
    for name, _, prefixes in CATEGORIES:
        if any(mime.startswith(p) for p in prefixes):
            return name
    return OTHER


def decoded_size(encoded: int, encoding: str) -> int:
    """Taille réelle sur disque : le base64 gonfle d'environ 37 % (avec les retours ligne)."""
    if encoding == "base64":
        return int(encoded * 57 / 78)
    return encoded


@dataclass
class Piece:
    chemin: str
    nom: str
    extension: str
    mime: str
    categorie: str
    taille_encodee: int
    taille: int
    statut: str


def _role(info: dict, parent: str, size: int) -> str | None:
    typ, sub, mime = info["type"], info["subtype"], info["mime"]
    filename, disp, cid = info["filename"], info["disposition"], info["cid"]

    if mime in IGNORED_MIME or extension(filename) in IGNORED_EXT:
        return None
    if typ == "text" and sub in ("plain", "html", "enriched") and not filename and disp != "attachment":
        return None  # corps du message
    if typ == "text" and sub == "calendar" and not filename and disp != "attachment":
        return None  # invitation intégrée au corps

    if typ == "image":
        if size < 15_000:
            return DECO  # icônes, pixels de suivi, logos
        if disp != "attachment":
            if size < 30_000:
                return DECO
            if (cid or parent == "related") and size < 150_000:
                return DECO  # logos et bannières de signatures / newsletters

    if filename or disp == "attachment":
        return PJ
    if typ in ("application", "audio", "video", "image", "model", "font", "message"):
        return PJ
    return None


def analyze_bodystructure(bs) -> list[Piece]:
    pieces: list[Piece] = []
    for path, part, parent in iter_leaf_parts(bs):
        info = leaf_info(part)
        size = decoded_size(info["size"], info["encoding"])
        role = _role(info, parent, size)
        if role is None:
            continue
        name = info["filename"]
        if not name:
            name = "message_joint.eml" if info["type"] == "message" else f"sans_nom ({info['mime']})"
        ext = extension(info["filename"]) or ("eml" if info["type"] == "message" else "")
        pieces.append(
            Piece(
                chemin=path,
                nom=name,
                extension=ext,
                mime=info["mime"],
                categorie=category(ext, info["mime"]),
                taille_encodee=info["size"],
                taille=size,
                statut=role,
            )
        )
    return pieces
