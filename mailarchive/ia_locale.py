"""Classement par une IA LOCALE (Ollama), sans aucun accès Internet.

- Les documents ne quittent jamais l'ordinateur : seul le serveur Ollama local (127.0.0.1) est contacté,
  toute autre adresse est refusée. Les documents confidentiels peuvent donc être traités.
- Prérequis (non installés par l'outil) : Ollama et au moins un modèle, par ex. `ollama pull gemma4`.
- Le modèle répond dans un format imposé (listes fermées de types et de domaines) avec un indice de confiance ;
  en dessous du seuil, le document reste « À classer ». Chaque déplacement est journalisé et annulable.
- Scans et photos : lus comme des images si le modèle en est capable (« vision »).
"""
from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import db, extract, progression
from .classify import extension
from .config import Config, load_raw, save_raw
from .themes import A_CLASSER, A_CLASSER_CONF, AUTRES, REGLES_PATH, TYPE_SECOURS, TRI_MANUEL

ADRESSE = "http://127.0.0.1:11434"
LIENS = {"Ollama (installation)": "https://ollama.com/download", "Modèles disponibles": "https://ollama.com/library"}
EXT_IMAGES = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
SEUIL_DEFAUT = 0.6
SECONDES_PAR_DOCUMENT = 5.0  # mesuré avec gemma4 (8 Md) sur une RTX 5070 Ti 12 Go ; affiné à chaque passage
TEXTE_MAX = 2500


class IALocaleIndisponible(RuntimeError):
    pass


def _verifier_local(url: str) -> None:
    hote = urlparse(url).hostname
    if hote not in ("127.0.0.1", "localhost", "::1"):
        raise PermissionError(f"Adresse refusée ({hote}) : l'IA locale doit tourner sur cet ordinateur.")


def _appel(chemin: str, data: dict | None = None, timeout: int = 600) -> dict:
    url = ADRESSE + chemin
    _verifier_local(url)
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _executable() -> str | None:
    trouve = shutil.which("ollama")
    if trouve:
        return trouve
    p = Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"
    return str(p) if p.exists() else None


def demarrer() -> str:
    """Vérifie qu'Ollama répond ; le démarre si besoin. Retourne sa version."""
    try:
        return _appel("/api/version", timeout=5).get("version", "?")
    except (urllib.error.URLError, OSError):
        pass
    exe = _executable()
    if not exe:
        raise IALocaleIndisponible("Ollama n'est pas installé sur cet ordinateur.")
    subprocess.Popen([exe, "serve"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1)
        try:
            return _appel("/api/version", timeout=5).get("version", "?")
        except (urllib.error.URLError, OSError):
            continue
    raise IALocaleIndisponible("Ollama est installé mais ne répond pas.")


@dataclass
class Modele:
    nom: str
    taille: int  # octets
    vision: bool
    parametres: str

    @property
    def memoire_necessaire_go(self) -> float:
        """Mémoire de carte graphique approximative : le modèle + ~20 % pour son contexte de travail."""
        return round(self.taille * 1.2 / 2**30, 1)


def modeles() -> list[Modele]:
    res = []
    for m in _appel("/api/tags", timeout=10).get("models", []):
        try:
            info = _appel("/api/show", {"model": m["name"]}, timeout=30)
        except Exception:
            info = {}
        res.append(Modele(nom=m["name"], taille=int(m.get("size", 0)), vision="vision" in (info.get("capabilities") or []),
                          parametres=(m.get("details") or {}).get("parameter_size", "")))
    return sorted(res, key=lambda m: m.taille)


def memoire_carte_graphique_go() -> float | None:
    """Mémoire de la carte graphique NVIDIA (la plus grande), ou None si inconnue."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        valeurs = [float(x) for x in r.stdout.split() if x.replace(".", "").isdigit()]
        return round(max(valeurs) / 1024, 1) if valeurs else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def reglages() -> dict:
    v = {"modele": "", "seuil": SEUIL_DEFAUT, "dans_veille": False, "secondes_par_document": SECONDES_PAR_DOCUMENT}
    v.update(load_raw().get("ia_locale") or {})
    return v


def enregistrer_reglages(v: dict) -> None:
    raw = load_raw()
    raw["ia_locale"] = v
    save_raw(raw)


def listes() -> tuple[list[str], list[str]]:
    regles = json.loads(REGLES_PATH.read_text(encoding="utf-8"))
    types = [t["type"] for t in regles.get("types", [])]
    types += [t for t in dict.fromkeys(TYPE_SECOURS.values()) if t not in types and not t.startswith("_")] + [AUTRES]
    domaines = sorted({r["theme"] for r in regles.get("regles", [])}) + [A_CLASSER]
    return types, domaines


def candidats(cfg: Config) -> list:
    """Documents dont le type ou le domaine est inconnu, confidentiels compris (rien ne quitte l'ordinateur)."""
    conn = db.ouvrir(cfg.base_index)
    lignes = conn.execute(
        "SELECT rowid AS id, * FROM fichiers WHERE type_manuel = 0 AND (theme IN (?, ?) OR type_doc = ?) "
        "AND COALESCE(type_doc, '') != ? GROUP BY chemin_local",
        (A_CLASSER, A_CLASSER_CONF, AUTRES, TRI_MANUEL)).fetchall()
    conn.close()
    return [l for l in lignes if Path(l["chemin_local"]).exists()]


def _image_png(data: bytes, ext: str) -> bytes | None:
    """Image (ou 1re page d'un PDF scanné) réduite à 1280 px, en PNG ; None si impossible."""
    try:
        from PIL import Image

        if ext == "pdf":
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(data)
            img = pdf[0].render(scale=1.6).to_pil()
            pdf.close()
        else:
            img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((1280, 1280))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return None


def _contexte(conn, f) -> tuple[str, str, str]:
    occ = conn.execute("SELECT nom_origine, objet, correspondant FROM pieces_recuperees WHERE sha256 = ?", (f["sha256"],)).fetchall()
    nom = (occ[0]["nom_origine"] if occ else "") or Path(f["chemin_local"]).name
    objets = " / ".join(sorted({o["objet"] or "" for o in occ if o["objet"]}))[:300]
    corr = (occ[0]["correspondant"] if occ else "") or ""
    return nom, objets, corr


def classer(modele: Modele, nom: str, objets: str, correspondant: str, texte: str, image: bytes | None,
            types: list[str], domaines: list[str], reflexion: bool = False) -> dict:
    """Type + domaine + confiance. Les listes sont dans la consigne ; la réponse est ensuite vérifiée strictement."""
    consigne = (
        "Tu es un assistant d'archivage de documents personnels. Classe le document ci-dessous.\n"
        f"- « type » : la nature du document, à choisir EXACTEMENT dans : {json.dumps(types, ensure_ascii=False)}\n"
        f"- « domaine » : son sujet, à choisir EXACTEMENT dans : {json.dumps(domaines, ensure_ascii=False)} "
        "(« À classer » si aucun ne convient vraiment).\n"
        "- « confiance » : entre 0 et 1. Sois prudent : en cas de doute, confiance basse.\n"
        'Réponds uniquement en JSON : {"type": "...", "domaine": "...", "confiance": 0.0}\n\n'
        f"Nom du fichier : {nom}\nObjet du mail : {objets or '—'}\nCorrespondant : {correspondant or '—'}\n"
        + (f"Texte (extrait) :\n{texte[:TEXTE_MAX]}" if texte else "Le document est fourni en image.")
    )
    data = {"model": modele.nom, "prompt": consigne, "stream": False, "format": "json", "think": reflexion,
            "options": {"temperature": 0, "num_ctx": 8192}}
    if image is not None:
        data["images"] = [base64.b64encode(image).decode()]
    try:
        r = _appel("/api/generate", data)
    except urllib.error.HTTPError as e:
        if e.code == 400:  # modèle sans mode « réflexion »
            data.pop("think", None)
            r = _appel("/api/generate", data)
        else:
            raise
    try:
        res = json.loads(r.get("response") or "{}")
    except json.JSONDecodeError:
        res = {}
    # Vérification stricte : une valeur hors liste ne déplace rien.
    if res.get("type") not in types:
        res["type"] = AUTRES
    if res.get("domaine") not in domaines:
        res["domaine"] = A_CLASSER
    try:
        res["confiance"] = max(0.0, min(1.0, float(res.get("confiance") or 0)))
    except (TypeError, ValueError):
        res["confiance"] = 0.0
    return {"type": res["type"], "domaine": res["domaine"], "confiance": res["confiance"]}


IMAGE_TEST_CODE = "4729"


def tester_vision(modele: Modele) -> bool:
    """Le modèle sait-il vraiment lire une image ? (lui faire lire un code sur une image de test)"""
    if not modele.vision:
        return False
    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.new("RGB", (900, 400), "white")
        d = ImageDraw.Draw(img)
        try:
            police = ImageFont.truetype("arial.ttf", 64)
        except OSError:
            police = ImageFont.load_default()
        d.text((60, 150), f"CODE {IMAGE_TEST_CODE}", fill="black", font=police)
        out = io.BytesIO()
        img.save(out, format="PNG")
        r = _appel("/api/generate", {"model": modele.nom, "prompt": "Quel code est écrit sur cette image ? Réponds seulement le code.",
                                     "images": [base64.b64encode(out.getvalue()).decode()], "stream": False, "think": False,
                                     "options": {"temperature": 0}}, timeout=300)
        return IMAGE_TEST_CODE in (r.get("response") or "")
    except Exception:
        return False


def proposer(cfg: Config, modele: Modele, limite: int | None = None, vision: bool | None = None,
             reflexion: bool = False) -> dict:
    """Propositions pour tous les candidats (rien n'est déplacé). Retourne aussi des statistiques.

    vision : le modèle lit-il vraiment les images ? (None = le vérifier avec tester_vision)."""
    types, domaines = listes()
    if vision is None:
        vision = tester_vision(modele)
    conn = db.ouvrir(cfg.base_index)
    liste = candidats(cfg)[:limite] if limite else candidats(cfg)
    props, illisibles, t0 = [], 0, time.monotonic()
    progression.r().dossier("Classement local", len(liste))
    try:
        for f in liste:
            nom, objets, corr = _contexte(conn, f)
            ext = extension(nom) or Path(f["chemin_local"]).suffix.lstrip(".").lower()
            data = Path(f["chemin_local"]).read_bytes()
            texte = extract.texte(data, ext)
            image = None
            if len(texte.strip()) < 80 and vision and (ext in EXT_IMAGES or ext == "pdf"):
                image = _image_png(data, ext)
            if len(texte.strip()) < 80 and image is None:
                illisibles += 1
                progression.r().avance(1)
                continue
            try:
                r = classer(modele, nom, objets, corr, texte, image, types, domaines, reflexion=reflexion)
            except Exception as e:  # un document en échec ne bloque pas les autres
                r = {"type": AUTRES, "domaine": A_CLASSER, "confiance": 0, "erreur": str(e)}
            props.append({"id": f["id"], "compte": f["compte"], "fichier": nom, "chemin": f["chemin_local"],
                          "confidentiel": bool(f["confidentiel"]), "lu_en_image": image is not None, **r})
            progression.r().avance(1)
    finally:
        progression.r().fin_dossier()
        conn.close()
    duree = time.monotonic() - t0
    traites = len(liste) - illisibles
    return {"propositions": props, "illisibles": illisibles, "duree_s": duree, "vision": vision,
            "secondes_par_document": duree / traites if traites else None}


def utiles(props: list[dict], seuil: float) -> list[dict]:
    """Propositions assez sûres et qui apportent quelque chose (pas « Autres documents / À classer »)."""
    return [p for p in props if p.get("confiance", 0) >= seuil and not p.get("erreur")
            and not (p["type"] == AUTRES and p["domaine"] == A_CLASSER)]


def appliquer(cfg: Config, props: list[dict], seuil: float) -> Path:
    from .tri_assiste import appliquer as appliquer_decisions, dossier_tri

    choisies = utiles(props, seuil)
    fichier = dossier_tri(cfg) / f"decisions_ia_locale_{time.strftime('%Y-%m-%d_%H%M%S')}.json"
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_text(json.dumps({"decisions": [{"id": p["id"], "type": p["type"], "domaine": p["domaine"]} for p in choisies]},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
    appliquer_decisions(cfg, fichier, autoriser_confidentiel=True)
    return fichier
