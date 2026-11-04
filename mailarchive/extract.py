"""Extraction du texte des documents (PDF, Word, Excel…) pour aider au classement.

Les fichiers viennent de mails : ils sont lus comme des données, jamais ouverts
ni exécutés. Les lectures sont plafonnées (taille, nombre de pages).
"""
from __future__ import annotations

import io
import logging
import re
import zipfile

MAX_CARACTERES = 6000
MAX_PAGES_PDF = 3
MAX_XML = 400_000

logging.getLogger("pypdf").setLevel(logging.ERROR)

_ZIP_XML = {
    "docx": ["word/document.xml"],
    "xlsx": ["xl/sharedStrings.xml"],
    "xlsm": ["xl/sharedStrings.xml"],
    "pptx": [f"ppt/slides/slide{i}.xml" for i in range(1, 4)],
    "odt": ["content.xml"],
    "ods": ["content.xml"],
    "odp": ["content.xml"],
}


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    lecteur = PdfReader(io.BytesIO(data), strict=False)
    if lecteur.is_encrypted:
        try:
            lecteur.decrypt("")
        except Exception:
            return ""
    morceaux = []
    for page in lecteur.pages[:MAX_PAGES_PDF]:
        morceaux.append(page.extract_text() or "")
        if sum(len(m) for m in morceaux) > MAX_CARACTERES:
            break
    return " ".join(morceaux)


def _zip_xml(data: bytes, membres: list[str]) -> str:
    morceaux = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        noms = set(zf.namelist())
        for m in membres:
            if m in noms:
                with zf.open(m) as f:
                    xml = f.read(MAX_XML).decode("utf-8", "replace")
                morceaux.append(re.sub(r"<[^>]+>", " ", xml))
    return " ".join(morceaux)


def texte(data: bytes, extension: str) -> str:
    """Texte du document (tronqué), ou chaîne vide si illisible ou non pris en charge."""
    ext = extension.lower()
    try:
        if ext == "pdf":
            t = _pdf(data)
        elif ext in _ZIP_XML:
            t = _zip_xml(data, _ZIP_XML[ext])
        elif ext in ("txt", "csv", "rtf", "htm", "html", "xml", "md"):
            t = data[: MAX_CARACTERES * 2].decode("utf-8", "replace")
            if ext in ("htm", "html", "xml", "rtf"):
                t = re.sub(r"<[^>]+>|\\[a-z]+-?\d* ?", " ", t)
        else:
            return ""
    except Exception:
        return ""
    return re.sub(r"\s+", " ", t)[:MAX_CARACTERES]
