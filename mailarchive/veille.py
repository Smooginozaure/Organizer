"""Veille automatique : scan à blanc puis récupération des nouveaux mails, planifiés par Windows.

La tâche « Archive mail - veille » du Planificateur de tâches lance veille.pyw (sans fenêtre).
Chaque exécution ajoute une ligne à _rapports/veille.log et ne garde que les rapports récents.
Toujours en lecture seule sur les serveurs, comme le reste de l'outil.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import db, progression
from .config import ROOT, Config, load_config, load_raw, save_raw

NOM_TACHE = "Archive mail - veille"
RAPPORTS_GARDES = 15
VERROU_PERIME_S = 6 * 3600
JOURS = {"lundi": "Monday", "mardi": "Tuesday", "mercredi": "Wednesday", "jeudi": "Thursday",
         "vendredi": "Friday", "samedi": "Saturday", "dimanche": "Sunday"}


class Occupe(RuntimeError):
    pass


@contextlib.contextmanager
def verrou(cfg: Config):
    """Empêche deux opérations simultanées (par ex. la veille pendant une récupération manuelle)."""
    chemin = cfg.base_index.parent / "operation_en_cours.lock"
    chemin.parent.mkdir(parents=True, exist_ok=True)
    if chemin.exists() and time.time() - chemin.stat().st_mtime > VERROU_PERIME_S:
        chemin.unlink(missing_ok=True)  # reste d'une opération interrompue brutalement
    try:
        fd = os.open(chemin, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Occupe("Une autre opération (scan, récupération ou veille) est déjà en cours.") from None
    try:
        os.write(fd, f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}".encode())
        os.close(fd)
        yield
    finally:
        chemin.unlink(missing_ok=True)


def reglages() -> dict:
    v = {"active": False, "frequence": "quotidienne", "heure": "08:00", "intervalle_h": 6, "jour": "lundi", "mode": "complet"}
    v.update(load_raw().get("veille") or {})
    return v


def enregistrer_reglages(v: dict) -> None:
    raw = load_raw()
    raw["veille"] = v
    save_raw(raw)


def description(v: dict) -> str:
    mode = "mail complet + pièces jointes" if v["mode"] == "complet" else "pièces jointes uniquement"
    if v["frequence"] == "horaire":
        quand = f"toutes les {v['intervalle_h']} h" if int(v["intervalle_h"]) > 1 else "toutes les heures"
    elif v["frequence"] == "hebdomadaire":
        quand = f"chaque {v['jour']} à {v['heure']}"
    else:
        quand = f"chaque jour à {v['heure']}"
    return f"{quand} — {mode}"


def _pythonw() -> str:
    p = Path(sys.executable)
    w = p.with_name("pythonw.exe")
    return str(w if w.exists() else p)


def _xml(v: dict) -> str:
    h, m = (int(x) for x in v["heure"].split(":"))
    debut = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
    if v["frequence"] == "horaire":
        declencheur = (f"<CalendarTrigger><StartBoundary>{debut}</StartBoundary><Repetition><Interval>PT{int(v['intervalle_h'])}H"
                       f"</Interval><Duration>P1D</Duration></Repetition><ScheduleByDay><DaysInterval>1</DaysInterval>"
                       f"</ScheduleByDay></CalendarTrigger>")
    elif v["frequence"] == "hebdomadaire":
        declencheur = (f"<CalendarTrigger><StartBoundary>{debut}</StartBoundary><ScheduleByWeek><DaysOfWeek>"
                       f"<{JOURS[v['jour']]} /></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger>")
    else:
        declencheur = (f"<CalendarTrigger><StartBoundary>{debut}</StartBoundary><ScheduleByDay><DaysInterval>1</DaysInterval>"
                       f"</ScheduleByDay></CalendarTrigger>")
    utilisateur = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
    script = ROOT / "veille.pyw"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Archive mail : scan et récupération automatiques des nouveaux mails (lecture seule, aucun mail supprimé).</Description></RegistrationInfo>
  <Triggers>{declencheur}</Triggers>
  <Principals><Principal id="Author"><UserId>{utilisateur}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{_pythonw()}</Command><Arguments>"{script}"</Arguments><WorkingDirectory>{ROOT}</WorkingDirectory></Exec></Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, encoding="cp850", errors="replace")


def activer(v: dict, nom: str = NOM_TACHE) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "tache.xml"
        f.write_text(_xml(v), encoding="utf-16")
        r = _schtasks("/Create", "/TN", nom, "/XML", str(f), "/F")
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    v["active"] = True
    if nom == NOM_TACHE:
        enregistrer_reglages(v)
    return True, "Veille activée : " + description(v)


def desactiver(nom: str = NOM_TACHE) -> tuple[bool, str]:
    r = _schtasks("/Delete", "/TN", nom, "/F")
    if nom == NOM_TACHE:
        v = reglages()
        v["active"] = False
        enregistrer_reglages(v)
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def etat(nom: str = NOM_TACHE) -> dict | None:
    """Prochaine et dernière exécution selon Windows, ou None si la tâche n'existe pas."""
    r = _schtasks("/Query", "/TN", nom, "/FO", "CSV", "/V")
    if r.returncode != 0:
        return None
    import csv
    import io

    lignes = list(csv.reader(io.StringIO(r.stdout)))
    if len(lignes) < 2:
        return {}
    entetes, valeurs = lignes[0], lignes[1]
    d = dict(zip(entetes, valeurs))
    trouver = lambda *cles: next((d[k] for k in d if any(c in k.lower() for c in cles)), "")
    derniere = trouver("dernière exécution", "last run time")
    derniere = "" if "1999" in derniere else derniere  # Windows affiche 30/11/1999 pour « jamais »
    return {"prochaine": trouver("prochaine", "next run"), "derniere": derniere,
            "resultat": trouver("dernier résultat", "last result")}


SCAN_INTEGRAL_JOURS = 30


def scan_integral_du(cfg: Config) -> bool:
    """Vrai si aucun scan intégral (tous les mails) n'a eu lieu depuis SCAN_INTEGRAL_JOURS jours."""
    conn = db.ouvrir(cfg.base_index)
    s = db.dernier_scan_integral(conn)
    conn.close()
    return s is None or (datetime.now() - datetime.fromisoformat(s["date"])).days >= SCAN_INTEGRAL_JOURS


def classement_local_veille(cfg: Config) -> str:
    """Si activé : classe les nouveaux « À classer » avec l'IA locale (hors ligne). Ne bloque jamais la veille."""
    from . import ia_locale

    r = ia_locale.reglages()
    if not r.get("dans_veille") or not r.get("modele"):
        return ""
    try:
        ia_locale.demarrer()
        modele = next((m for m in ia_locale.modeles() if m.nom == r["modele"]), None)
        if modele is None:
            return f", IA locale : modèle {r['modele']} introuvable"
        if not ia_locale.candidats(cfg):
            return ""
        res = ia_locale.proposer(cfg, modele, reflexion=bool(r.get("reflexion")))
        choisies = ia_locale.utiles(res["propositions"], float(r.get("seuil", ia_locale.SEUIL_DEFAUT)))
        if choisies:
            ia_locale.appliquer(cfg, res["propositions"], float(r.get("seuil", ia_locale.SEUIL_DEFAUT)))
        return f", IA locale : {len(choisies)} document(s) classé(s)"
    except Exception as e:  # noqa: BLE001
        return f", IA locale indisponible ({type(e).__name__})"


def journal(cfg: Config) -> Path:
    return cfg.dossier_rapports / "veille.log"


def _elaguer(cfg: Config) -> None:
    for motif in ("scan_a_blanc_*", "recuperation_*"):
        dossiers = sorted(cfg.dossier_rapports.glob(motif))
        for vieux in dossiers[:-RAPPORTS_GARDES]:
            shutil.rmtree(vieux, ignore_errors=True)


def executer(cfg: Config | None = None) -> int:
    """Une exécution de la veille (appelée par le Planificateur de tâches ou depuis le menu)."""
    from . import recuperation, scan

    cfg = cfg or load_config()
    v = reglages()
    cfg.dossier_rapports.mkdir(parents=True, exist_ok=True)
    log = journal(cfg)
    debut = datetime.now()
    ancien = progression.utiliser(progression.Silencieux())
    try:
        with verrou(cfg), open(log, "a", encoding="utf-8") as jf:
            with contextlib.redirect_stdout(jf), contextlib.redirect_stderr(jf):
                scan.lancer(cfg, ouvrir=False, afficher_resume=False, rapide=not scan_integral_du(cfg))
                recuperation.lancer(cfg, ouvrir=False, sauvegarder=(v["mode"] == "complet"))
                ia = classement_local_veille(cfg)
            b = recuperation.DERNIER_BILAN
            ligne = (f"{debut:%Y-%m-%d %H:%M} | veille OK | {b.pj_classees} nouvelle(s) pièce(s) jointe(s), "
                     f"{b.mails_sauvegardes} mail(s) sauvegardé(s), {len(b.erreurs)} incident(s){ia} | "
                     f"{(datetime.now() - debut).seconds // 60} min")
            jf.write(ligne + "\n")
        _elaguer(cfg)
        return 0
    except Occupe as e:
        with open(log, "a", encoding="utf-8") as jf:
            jf.write(f"{debut:%Y-%m-%d %H:%M} | veille reportée | {e}\n")
        return 0
    except Exception as e:  # noqa: BLE001 — tout incident doit être journalisé, pas perdu
        with open(log, "a", encoding="utf-8") as jf:
            jf.write(f"{debut:%Y-%m-%d %H:%M} | ÉCHEC | {type(e).__name__}: {e}\n")
        return 1
    finally:
        progression.utiliser(ancien)


def dernieres_lignes(cfg: Config, n: int = 8) -> list[str]:
    log = journal(cfg)
    if not log.exists():
        return []
    lignes = [l for l in log.read_text(encoding="utf-8", errors="replace").splitlines() if " | " in l]
    return lignes[-n:]
