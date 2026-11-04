"""Interface « graphique » dans l'invite de commande (lancée par « Trier Mails.bat »)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)
from rich.prompt import Confirm as _Confirm
from rich.prompt import Prompt
from rich.table import Table

from . import __version__, comptes, credentials, db, progression, recherche, recuperation, scan, veille
from .config import desktop_dir, dossier_retenu, load_config, load_raw, save_raw
from .rapport_html import nombre, taille_lisible
from .themes import A_CLASSER, A_CLASSER_CONF, REGLES_PATH

console = Console(highlight=False)


class Confirm(_Confirm):
    """Questions oui/non en français : o / n."""
    choices = ["o", "n"]
    validate_error_message = "[prompt.invalid]Répondez o (oui) ou n (non)"

    def process_response(self, value: str) -> bool:
        v = value.strip().lower()
        if v in ("o", "oui", "y", "yes"):
            return True
        if v in ("n", "non", "no"):
            return False
        from rich.prompt import InvalidResponse

        raise InvalidResponse(self.validate_error_message)

    def render_default(self, default):
        return f"({'o' if default else 'n'})"


Prompt.illegal_choice_message = "[prompt.invalid.choice]Choisissez une des options proposées"
SCAN_ANCIEN_JOURS = 7


# ───────────────────────── éléments communs ─────────────────────────

class RichRapporteur(progression.Rapporteur):
    """Barres de progression pour le scan et la récupération."""

    def __init__(self):
        super().__init__()
        self.progress: Progress | None = None
        self.tache = None

    def titre(self, texte):
        self.fin_dossier()
        console.rule(f"[bold cyan]{texte}")

    def alerte(self, texte):
        console.print(f"[yellow]⚠ {texte}")

    def dossier(self, nom, total, detail=""):
        self.fin_dossier()
        self.progress = Progress(SpinnerColumn(), TextColumn("{task.description}", style="bold"), BarColumn(bar_width=36),
                                 MofNCompleteColumn(), TextColumn("mails"), TimeElapsedColumn(), TimeRemainingColumn(),
                                 console=console)
        self.progress.start()
        self.tache = self.progress.add_task(f"{nom[:28]:<28}", total=max(total, 1), completed=0 if total else 1)

    def avance(self, n):
        if self.progress is not None:
            self.progress.advance(self.tache, n)

    def fin_dossier(self):
        if self.progress is not None:
            self.progress.stop()
            self.progress = None

    def texte(self, texte):
        pass  # l'interface affiche ses propres résumés


def pause():
    Prompt.ask("\n[dim]Appuyez sur Entrée pour revenir[/]", default="", show_default=False)


def succes(titre: str, lignes: list[str] | str = ""):
    corps = "\n".join(lignes) if isinstance(lignes, list) else lignes
    console.print(Panel(corps or titre, title=f"[bold green]✓ {titre}", border_style="green", padding=(1, 2)))


def echec(titre: str, lignes: list[str] | str = ""):
    corps = "\n".join(lignes) if isinstance(lignes, list) else lignes
    console.print(Panel(corps or titre, title=f"[bold red]✗ {titre}", border_style="red", padding=(1, 2)))


def info(titre: str, lignes: list[str] | str):
    corps = "\n".join(lignes) if isinstance(lignes, list) else lignes
    console.print(Panel(corps, title=f"[bold]{titre}", border_style="cyan", padding=(1, 2)))


def menu(titre: str, options: list[tuple[str, str, str]], retour: str = "Retour") -> str:
    """options : (touche, libellé, aide). Les options dont l'aide commence par « 🔒 » sont verrouillées."""
    t = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0, 2), title=f"[bold]{titre}", title_justify="left")
    t.add_column(style="bold cyan", justify="right")
    t.add_column()
    t.add_column(style="dim")
    for touche, libelle, aide in options:
        verrou = aide.startswith("🔒")
        t.add_row(touche, f"[dim]{libelle}[/]" if verrou else libelle, aide)
    t.add_row("0", retour, "")
    console.print(t)
    return Prompt.ask("[bold]Votre choix", choices=[o[0] for o in options] + ["0"], show_choices=False)


def il_y_a(d: datetime) -> str:
    s = (datetime.now() - d).total_seconds()
    if s < 3600:
        return f"il y a {int(s // 60)} min"
    if s < 86400:
        return f"il y a {int(s // 3600)} h"
    return f"il y a {int(s // 86400)} jour(s)"


def espace_libre(chemin: Path) -> int:
    p = chemin
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def entete():
    console.clear()
    console.print(Panel.fit(
        f"[bold]TRIER MAILS[/] [dim]v{__version__}[/]  —  sauvegarde et classement de vos pièces jointes\n"
        "[green]Vos mails ne sont jamais modifiés, déplacés ni supprimés sur les serveurs.[/]",
        border_style="cyan", padding=(0, 2)))


def dernier_scan(cfg):
    conn = db.ouvrir(cfg.base_index)
    s = db.dernier_scan_complet(conn)
    conn.close()
    return s


def etat(cfg) -> Table:
    conn = db.ouvrir(cfg.base_index)
    s = db.dernier_scan_complet(conn)
    nb, octets = conn.execute("SELECT COUNT(DISTINCT chemin_local), COALESCE(SUM(taille), 0) FROM "
                              "(SELECT chemin_local, MAX(taille) taille FROM fichiers GROUP BY chemin_local)").fetchone()
    nb_eml = conn.execute("SELECT COUNT(*) FROM mails_recuperes WHERE fichier_eml IS NOT NULL").fetchone()[0]
    conn.close()
    t = Table(box=box.ROUNDED, show_header=False, padding=(0, 1), expand=False)
    t.add_column(style="bold", min_width=14)
    t.add_column()
    boites = "  ".join(f"[green]✓[/] {c.nom}" if credentials.get_password(c) else f"[red]✗[/] {c.nom} (mot de passe manquant)"
                       for c in cfg.comptes) or "[yellow]aucune — commencez par le choix 1[/]"
    t.add_row("Boîtes mail", boites)
    if s:
        d = datetime.fromisoformat(s["date"])
        age = f"[yellow]{il_y_a(d)} — pensez à le refaire[/]" if datetime.now() - d > timedelta(days=SCAN_ANCIEN_JOURS) else il_y_a(d)
        genre = "rapide" if s["mode"] == "incremental" else "complet"
        t.add_row("Dernier scan", f"{d:%d/%m/%Y %H:%M} — {genre} ({age})")
    else:
        t.add_row("Dernier scan", "[yellow]aucun — obligatoire avant la récupération[/]")
    t.add_row("Archive", f"{nombre(nb)} documents ({taille_lisible(octets)}) · {nombre(nb_eml)} mails sauvegardés\n"
                         f"[dim]{cfg.dossier_archive} — {taille_lisible(espace_libre(cfg.dossier_archive))} libres[/]")
    v = veille.reglages()
    t.add_row("Veille", f"[green]active[/] — {veille.description(v)}" if v.get("active") else "[dim]désactivée[/]")
    return t


# ───────────────────────── 1. boîtes mail ─────────────────────────

def ecran_boites():
    while True:
        cfg = load_config()
        entete()
        s = dernier_scan(cfg)
        t = Table(title="[bold]1 · Vos boîtes mail", title_justify="left", box=box.ROUNDED)
        for col in ("#", "Boîte", "Adresse", "Serveur", "Mot de passe", "Dossiers récupérés"):
            t.add_column(col)
        for i, c in enumerate(cfg.comptes, 1):
            dossiers = "—"
            if s:
                conn = db.ouvrir(cfg.base_index)
                ds = conn.execute("SELECT role, nom FROM dossiers WHERE scan_id = ? AND compte = ?", (s["id"], c.nom)).fetchall()
                conn.close()
                dossiers = f"{sum(dossier_retenu(cfg.recuperation, d['role'], d['nom']) for d in ds)} / {len(ds)}"
            t.add_row(str(i), c.nom, c.email, c.hote, "[green]✓ enregistré" if credentials.get_password(c) else "[red]✗ manquant", dossiers)
        if not cfg.comptes:
            t.add_row("", "[yellow]aucune boîte paramétrée", "", "", "", "")
        console.print(t)
        choix = menu("Que voulez-vous faire ?", [
            ("1", "Ajouter une boîte mail", "iCloud, Free, Gmail, Orange, La Poste, SFR…"),
            ("2", "Tester toutes les connexions", ""),
            ("3", "Changer un mot de passe", ""),
            ("4", "Choisir les dossiers à récupérer", "Réception, Envoyés, Brouillons…" if s else "🔒 après un scan"),
            ("5", "Retirer une boîte mail", "la boîte et l'archive ne sont pas touchées"),
        ])
        if choix == "0":
            return
        if choix == "1":
            ajouter_boite()
        elif choix == "2":
            tester_connexions(cfg)
        elif choix in ("3", "5") and cfg.comptes:
            n = Prompt.ask("Numéro de la boîte", choices=[str(i) for i in range(1, len(cfg.comptes) + 1)])
            c = cfg.comptes[int(n) - 1]
            if choix == "3":
                ajouter_boite(c.email)
            elif Confirm.ask(f"Retirer {c.email} de l'outil ?", default=False):
                comptes.supprimer(c.email)
                succes("Boîte retirée", f"{c.email} n'est plus suivie. Votre boîte mail et votre archive sont intactes.")
                pause()
        elif choix == "4" and s:
            choisir_dossiers(cfg, s)


def ajouter_boite(email: str | None = None):
    console.print()
    email = email or Prompt.ask("[bold]Adresse e-mail").strip()
    if not email:
        return
    ok = comptes.enregistrer(email)
    cfg = load_config()
    c = next((c for c in cfg.comptes if c.email.lower() == email.lower()), None)
    if ok and c:
        succes("Paramétrage effectué — connexion réussie",
               [f"Boîte : [bold]{c.nom}[/]  ({c.email})", f"Serveur : {c.hote}",
                "Mot de passe enregistré dans le Gestionnaire d'identifiants Windows.",
                "", "[dim]Étape suivante : 2 · Scan à blanc.[/]"])
    else:
        echec("Paramétrage non effectué", "La connexion n'a pas abouti : rien n'a été enregistré. Vérifiez l'adresse et le mot de passe.")
    pause()


def tester_connexions(cfg):
    t = Table(title="[bold]Test des connexions", title_justify="left", box=box.ROUNDED)
    t.add_column("Boîte")
    t.add_column("Résultat")
    with console.status("Connexion aux serveurs…"):
        for c in cfg.comptes:
            pwd = credentials.get_password(c)
            if not pwd:
                t.add_row(c.nom, "[red]✗ mot de passe manquant")
                continue
            ok, message, _ = comptes.tester(c, pwd)
            t.add_row(c.nom, ("[green]✓ " if ok else "[red]✗ ") + message)
    console.print(t)
    pause()


def choisir_dossiers(cfg, s):
    conn = db.ouvrir(cfg.base_index)
    ds = conn.execute("SELECT compte, nom, role, messages FROM dossiers WHERE scan_id = ? ORDER BY compte, messages DESC", (s["id"],)).fetchall()
    conn.close()
    while True:
        cfg = load_config()
        t = Table(title="[bold]Dossiers récupérés", title_justify="left", box=box.ROUNDED)
        for col in ("#", "", "Boîte", "Dossier", "Rôle", "Mails"):
            t.add_column(col)
        for i, d in enumerate(ds, 1):
            ret = dossier_retenu(cfg.recuperation, d["role"], d["nom"])
            t.add_row(str(i), "[green]☑" if ret else "☐", d["compte"], d["nom"], scan.ROLES_LISIBLES.get(d["role"], d["role"]), nombre(d["messages"] or 0))
        console.print(t)
        console.print("[dim]Un dossier portant le même nom dans plusieurs boîtes (ex. « Sent ») change pour toutes.[/]")
        rep = Prompt.ask("Numéros à cocher/décocher (ex. 3 7), ou Entrée pour terminer", default="", show_default=False)
        if not rep.strip():
            return
        raw = load_raw()
        r = raw.setdefault("recuperation", dict(cfg.recuperation))
        inclus = {x.lower() for x in r.get("dossiers_inclus", [])}
        exclus = {x.lower() for x in r.get("dossiers_exclus", [])}
        for n in rep.replace(",", " ").split():
            if n.isdigit() and 1 <= int(n) <= len(ds):
                d = ds[int(n) - 1]
                nom = d["nom"].lower()
                if dossier_retenu(cfg.recuperation, d["role"], d["nom"]):
                    inclus.discard(nom)
                    exclus.add(nom)
                else:
                    exclus.discard(nom)
                    inclus.add(nom)
        r["dossiers_inclus"], r["dossiers_exclus"] = sorted(inclus), sorted(exclus)
        save_raw(raw)
        console.clear()


# ───────────────────────── 2. scan à blanc ─────────────────────────

def ecran_scan(cfg):
    entete()
    info("2 · Scan à blanc", [
        "Le scan lit la [bold]structure des mails de tous les dossiers[/] de vos boîtes",
        "(réception, envoyés, archives, corbeille…) : types, noms et tailles des pièces jointes.",
        "[bold]Rien n'est téléchargé, aucun mail n'est modifié.[/] Il est obligatoire avant la récupération."])
    manquants = [c.nom for c in cfg.comptes if not credentials.get_password(c)]
    if manquants:
        console.print(f"[yellow]⚠ Mot de passe manquant pour : {', '.join(manquants)} — ces boîtes seront ignorées.")
    avant = dernier_scan(cfg)
    rapide = False
    if avant:
        conn = db.ouvrir(cfg.base_index)
        integral = db.dernier_scan_integral(conn)
        conn.close()
        d = datetime.fromisoformat(avant["date"])
        integral_ancien = integral is None or datetime.now() - datetime.fromisoformat(integral["date"]) > timedelta(days=veille.SCAN_INTEGRAL_JOURS)
        conseil_rapide = "" if integral_ancien else "  [green](recommandé)[/]"
        conseil_complet = ("  [yellow](recommandé : dernier scan complet il y a plus de 30 jours)[/]" if integral_ancien
                           else "  [dim]conseillé une fois par mois[/]")
        choix = menu("Quel scan ?", [
            ("1", f"Scan rapide — uniquement les mails arrivés depuis le {d:%d/%m/%Y à %H:%M}{conseil_rapide}",
             "quelques secondes"),
            ("2", f"Scan complet — relit tous les mails{conseil_complet}", "2 à 5 minutes"),
        ], retour="Annuler")
        if choix == "0":
            return
        rapide = choix == "1"
    elif not Confirm.ask("Premier scan : il lit tous les mails (2 à 5 minutes). Lancer ?", default=True):
        return
    rap = RichRapporteur()
    ancien = progression.utiliser(rap)
    try:
        with veille.verrou(cfg):
            dossier = scan.lancer(cfg, ouvrir=True, afficher_resume=False, rapide=rapide)
    except veille.Occupe as e:
        echec("Scan impossible pour l'instant", str(e))
        return pause()
    except KeyboardInterrupt:
        rap.fin_dossier()
        echec("Scan interrompu", "Aucun changement : relancez-le quand vous voulez.")
        return pause()
    finally:
        rap.fin_dossier()
        progression.utiliser(ancien)

    r = json.loads((dossier / "resume.json").read_text(encoding="utf-8"))
    lignes = [f"[bold]{nombre(r['mails'])}[/] mails analysés ({taille_lisible(r['volume_boites_octets'])}) dans toutes vos boîtes",
              f"Pièces jointes utiles (sans doublons, hors corbeille/spam) : [bold]{taille_lisible(r['pj_utiles_octets'])}[/]"]
    if r.get("mode") == "incremental":
        lignes.insert(0, f"[bold]Scan rapide[/] : {nombre(r['nouveaux'])} nouveau(x) mail(s) analysé(s), "
                         f"{nombre(r['supprimes'])} supprimé(s) ou déplacé(s) depuis le scan précédent")
    elif avant:
        conn = db.ouvrir(cfg.base_index)
        n_avant = conn.execute("SELECT COUNT(*) FROM messages WHERE scan_id = ?", (avant["id"],)).fetchone()[0]
        nouveau = db.dernier_scan_complet(conn)
        n_apres = conn.execute("SELECT COUNT(*) FROM messages WHERE scan_id = ?", (nouveau["id"],)).fetchone()[0] if nouveau else n_avant
        conn.close()
        diff = n_apres - n_avant
        lignes.append(f"Depuis le scan précédent : [bold]{'+' if diff >= 0 else ''}{nombre(diff)}[/] mails")
    cplt, pj = recuperation.reste_a_faire(cfg, True), recuperation.reste_a_faire(cfg, False)
    if premiere_recuperation(cfg):
        lignes += ["", f"⏱ {DUREE_PREMIERE_RECUPERATION}"]
    lignes += ["", "[bold]Reste à récupérer :[/]",
               f"  1 · Mail complet + pièces jointes : {taille_lisible(cplt['octets_mails'] + cplt['octets_pj'])} "
               f"({nombre(cplt['mails'])} mails, {nombre(cplt['pj'])} pièces jointes)",
               f"  2 · Pièces jointes uniquement     : {taille_lisible(pj['octets_pj'])} ({nombre(pj['pj'])} pièces jointes)",
               f"[dim]Espace libre sur le disque : {taille_lisible(espace_libre(cfg.dossier_archive))}[/]"]
    if r.get("erreurs"):
        lignes += ["", f"[yellow]⚠ {len(r['erreurs'])} incident(s) : voir le rapport.[/]"]
    succes("Scan à blanc terminé", lignes)
    pause()


# ───────────────────────── 3. récupération ─────────────────────────

def ecran_recuperation(cfg):
    entete()
    s = dernier_scan(cfg)
    if not s:
        echec("Scan à blanc obligatoire", "Lancez d'abord le choix 2 · Scan à blanc : la récupération s'appuie dessus.")
        return pause()
    d = datetime.fromisoformat(s["date"])
    if datetime.now() - d > timedelta(days=SCAN_ANCIEN_JOURS):
        console.print(f"[yellow]⚠ Le dernier scan date du {d:%d/%m/%Y} : les mails reçus depuis ne seront pas récupérés.")
        if Confirm.ask("Refaire un scan à blanc d'abord ?", default=True):
            ecran_scan(cfg)
            cfg = load_config()
            entete()
    with console.status("Calcul de ce qui reste à récupérer…"):
        cplt, pj = recuperation.reste_a_faire(cfg, True), recuperation.reste_a_faire(cfg, False)
    t = Table(title=f"[bold]3 · Récupération[/]  [dim](scan du {d:%d/%m/%Y %H:%M})", title_justify="left", box=box.ROUNDED)
    for col in ("", "Mode", "Ce qui sera téléchargé", "Espace"):
        t.add_column(col)
    t.add_row("1", "[bold]Mail complet + pièces jointes", f"{nombre(cplt['mails'])} mails (.eml) et {nombre(cplt['pj'])} pièces jointes",
              taille_lisible(cplt["octets_mails"] + cplt["octets_pj"]))
    t.add_row("2", "[bold]Pièces jointes uniquement", f"{nombre(pj['pj'])} pièces jointes", taille_lisible(pj["octets_pj"]))
    console.print(t)
    console.print(f"[dim]Dossiers inclus : {', '.join(cplt['dossiers']) or 'aucun'} — modifiable dans 1 · Boîtes mail.[/]")
    if premiere_recuperation(cfg):
        console.print(Panel(DUREE_PREMIERE_RECUPERATION, title="[bold]⏱ Durée", border_style="yellow", padding=(0, 2)))
    if not (cplt["mails"] or cplt["pj"]):
        console.print("[green]✓ Tout ce que le dernier scan a inventorié est déjà récupéré.[/] "
                      "Pour les mails arrivés depuis, refaites un scan (2) — ou activez la veille (5).")
    mode = Prompt.ask("[bold]Votre choix", choices=["1", "2", "0"], show_choices=False)
    if mode == "0":
        return
    sauvegarder = mode == "1"

    periode = menu("Période", [("1", "Tout l'historique", ""), ("2", "Les 12 derniers mois", ""), ("3", "Depuis une date précise", "")],
                   retour="Annuler")
    if periode == "0":
        return
    depuis = None
    if periode == "2":
        depuis = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    elif periode == "3":
        while True:
            saisie = Prompt.ask("Date de début (JJ/MM/AAAA)")
            try:
                depuis = datetime.strptime(saisie.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
                break
            except ValueError:
                console.print("[red]Format attendu : 15/01/2024")
    est = recuperation.reste_a_faire(cfg, sauvegarder, depuis)
    besoin = est["octets_pj"] + (est["octets_mails"] if sauvegarder else 0)
    libre = espace_libre(cfg.dossier_archive)
    if besoin * 1.05 > libre:
        echec("Espace disque insuffisant", f"Il faut environ {taille_lisible(besoin)} ; seulement {taille_lisible(libre)} libres. "
                                          "Changez l'emplacement de l'archive (choix 4) ou libérez de la place.")
        return pause()
    console.print(f"\nÀ télécharger : [bold]{taille_lisible(besoin)}[/]  →  {cfg.dossier_archive}")
    console.print("[dim]Interruption possible à tout moment (Ctrl+C) : la reprise saute ce qui est déjà fait.[/]")
    if not Confirm.ask("Lancer la récupération ?", default=True):
        return
    raw = load_raw()
    raw.setdefault("recuperation", {})["sauvegarder_mails"] = sauvegarder  # mémorisé comme mode par défaut
    save_raw(raw)

    rap = RichRapporteur()
    ancien = progression.utiliser(rap)
    try:
        with veille.verrou(cfg):
            recuperation.lancer(load_config(), ouvrir=True, sauvegarder=sauvegarder, depuis=depuis)
    except veille.Occupe as e:
        echec("Récupération impossible pour l'instant", str(e))
        return pause()
    except KeyboardInterrupt:
        rap.fin_dossier()
        echec("Récupération interrompue", "Ce qui a été récupéré est conservé. Relancez le choix 3 pour continuer.")
        return pause()
    finally:
        rap.fin_dossier()
        progression.utiliser(ancien)
    b = recuperation.DERNIER_BILAN
    lignes = [f"Pièces jointes classées : [bold]{nombre(b.pj_classees)}[/] ({taille_lisible(b.octets_pj)})",
              f"Mails sauvegardés (.eml) : [bold]{nombre(b.mails_sauvegardes)}[/] ({taille_lisible(b.octets_mails)})",
              f"Doublons évités : {nombre(b.doublons)}   ·   À classer : {nombre(b.a_classer)}   ·   Confidentiels (tri local) : {nombre(b.confidentiels)}"]
    if b.mails_disparus:
        lignes.append(f"[dim]{b.mails_disparus} mail(s) supprimé(s) du serveur depuis le scan, ignoré(s).[/]")
    if b.erreurs:
        lignes.append(f"[yellow]⚠ {len(b.erreurs)} incident(s) : voir le rapport.[/]")
    succes("Récupération terminée — aucun mail modifié sur les serveurs", lignes)
    pause()


# ───────────────────────── 4. tri ─────────────────────────

def dossier_documents() -> Path:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return Path(os.path.expandvars(winreg.QueryValueEx(k, "Personal")[0]))
    except Exception:
        return Path.home() / "Documents"


def ecran_tri():
    while True:
        cfg = load_config()
        entete()
        conn = db.ouvrir(cfg.base_index)
        types = conn.execute("SELECT COALESCE(type_doc, 'Autres documents') t, COUNT(DISTINCT chemin_local) n, SUM(taille) o "
                             "FROM fichiers GROUP BY t ORDER BY n DESC").fetchall()
        a_classer = conn.execute("SELECT COUNT(DISTINCT chemin_local) FROM fichiers WHERE theme IN (?, ?) AND "
                                 "COALESCE(type_doc, 'Autres documents') = 'Autres documents'", (A_CLASSER, A_CLASSER_CONF)).fetchone()[0]
        conn.close()
        t = Table(title=f"[bold]4 · Tri et rangement[/]  [dim]{cfg.dossier_archive} — {taille_lisible(espace_libre(cfg.dossier_archive))} libres",
                  title_justify="left", box=box.ROUNDED)
        t.add_column("Type de document")
        t.add_column("Documents", justify="right")
        t.add_column("Volume", justify="right")
        for r in types:
            t.add_row(r["t"], nombre(r["n"]), taille_lisible(r["o"] or 0))
        console.print(t)
        console.print(f"[dim]Rangement : <Type>\\<Domaine>\\<Sous-domaine>\\<Année> — {nombre(a_classer)} document(s) à classer.[/]")
        choix = menu("Que voulez-vous faire ?", [
            ("1", "Changer l'emplacement de l'archive", "Bureau, Documents, disque externe…"),
            ("2", "Voir les règles de tri", "types et domaines reconnus"),
            ("3", "Modifier les règles de tri", "ouvre le Bloc-notes"),
            ("4", "Mots à exclure du tri", "mots ignorés, à trier soi-même, confidentiels"),
            ("5", "Aperçu du rangement", "simulation : rien n'est déplacé"),
            ("6", "Appliquer le rangement", "re-range avec les règles à jour"),
            ("7", "Classer les documents restants avec une IA", "agent IA en ligne, ou IA locale hors ligne"),
            ("8", "Annuler le dernier classement par IA", ""),
            ("9", "Ouvrir l'archive dans l'Explorateur", ""),
        ])
        if choix == "0":
            return
        {"1": changer_emplacement, "2": voir_regles, "3": modifier_regles, "4": ecran_exclusions, "5": apercu,
         "6": appliquer_rangement, "7": ecran_ia, "8": annuler_tri,
         "9": lambda: os.startfile(str(cfg.dossier_archive))}[choix]()


def changer_emplacement():
    cfg = load_config()
    console.print(f"\nEmplacement actuel : [bold]{cfg.dossier_archive}")
    choix = menu("Nouvel emplacement", [
        ("1", "Bureau", str(desktop_dir() / "Archive mail")),
        ("2", "Documents", str(dossier_documents() / "Archive mail")),
        ("3", "Autre (disque externe, NAS…)", "chemin à saisir"),
    ], retour="Annuler")
    if choix == "0":
        return
    cible = {"1": desktop_dir() / "Archive mail", "2": dossier_documents() / "Archive mail"}.get(choix)
    if cible is None:
        saisie = Prompt.ask("Chemin complet du dossier (ex. E:\\Archive mail)").strip().strip('"')
        if not saisie:
            return
        cible = Path(saisie)
    if (cfg.base_index.parent / "operation_en_cours.lock").exists():
        echec("Déplacement impossible pour l'instant", "Une opération (scan, récupération ou veille) est en cours.")
        return pause()
    if not Confirm.ask(f"Déplacer l'archive vers [bold]{cible}[/] ?", default=True):
        return
    from . import emplacement

    try:
        with console.status("Déplacement de l'archive…"):
            message = emplacement.deplacer_archive(cfg.dossier_archive, cible)
        succes("Emplacement modifié", message)
    except Exception as e:
        echec("Déplacement annulé", f"{e}\nVotre archive n'a pas bougé.")
    pause()


def voir_regles():
    regles = json.loads(REGLES_PATH.read_text(encoding="utf-8"))
    t = Table(title="[bold]Types de document (1er niveau)", title_justify="left", box=box.ROUNDED)
    t.add_column("Type")
    t.add_column("Reconnu par exemple grâce à…", style="dim")
    for r in regles.get("types", []):
        t.add_row(r["type"], ", ".join(r["mots"][:6]))
    console.print(t)
    t = Table(title="[bold]Domaines (niveaux suivants)", title_justify="left", box=box.ROUNDED)
    t.add_column("Domaine / sous-domaine")
    t.add_column("Mots-clés", style="dim")
    t.add_column("Expéditeurs", style="dim")
    for r in sorted(regles.get("regles", []), key=lambda r: r["theme"]):
        t.add_row(r["theme"], ", ".join(r.get("mots", [])[:5]), ", ".join(r.get("expediteurs", [])[:3]))
    console.print(t)
    console.print("[dim]Sans règle reconnue : Photos et vidéos, Tableaux et listes, Agenda et contacts… ou « À classer ».[/]")
    pause()


def modifier_regles():
    subprocess.Popen(["notepad.exe", str(REGLES_PATH)])
    info("Règles ouvertes dans le Bloc-notes", [
        "Ajoutez un mot-clé ou un expéditeur dans la règle voulue, puis enregistrez.",
        "Ensuite : [bold]5 · Aperçu[/] pour vérifier, puis [bold]6 · Appliquer[/] pour re-ranger l'archive."])
    pause()


LISTES_EXCLUSION = {
    # clé : (section de config.json, sous-clé, libellé, effet)
    "ignorer": ("exclusions", "ignorer", "Mots ignorés",
                "ne comptent jamais pour les règles (signature, pied de page, mot trompeur…)"),
    "a_trier_soi_meme": ("exclusions", "a_trier_soi_meme", "À trier soi-même",
                         "le document va dans « À trier soi-même » : ni règles, ni IA"),
    "confidentiel": ("confidentiel", "mots", "Confidentiels",
                     "jamais envoyé à une IA en ligne ; s'ajoute à la liste intégrée (gendarmerie, diplomatie…)"),
}


def _mots(raw: dict, cle: str) -> list[str]:
    section, sous_cle, _, _ = LISTES_EXCLUSION[cle]
    return [m for m in (raw.get(section) or {}).get(sous_cle, []) if str(m).strip()]


def _enregistrer_mots(raw: dict, cle: str, mots: list[str]) -> None:
    section, sous_cle, _, _ = LISTES_EXCLUSION[cle]
    raw.setdefault(section, {})[sous_cle] = mots
    save_raw(raw)


def saisir_mots(texte: str) -> list[str]:
    """« abc, def ; ghi » → ['abc', 'def', 'ghi'] (sans doublons, casse et accents ignorés pour la comparaison)."""
    from .themes import normaliser

    vus, res = set(), []
    for m in texte.replace(";", ",").replace("\n", ",").split(","):
        m = " ".join(m.split())
        if m and normaliser(m).strip() and normaliser(m) not in vus:
            vus.add(normaliser(m))
            res.append(m)
    return res


def ajouter_mots(raw: dict, cle: str, nouveaux: list[str]) -> list[str]:
    """Ajoute à la liste ; retourne les mots réellement ajoutés."""
    from .themes import normaliser

    actuels = _mots(raw, cle)
    connus = {normaliser(m) for m in actuels}
    ajout = [m for m in nouveaux if normaliser(m) not in connus]
    if ajout:
        _enregistrer_mots(raw, cle, actuels + ajout)
    return ajout


def retirer_mots(raw: dict, cle: str, a_retirer: list[str]) -> list[str]:
    from .themes import normaliser

    cibles = {normaliser(m) for m in a_retirer}
    actuels = _mots(raw, cle)
    retires = [m for m in actuels if normaliser(m) in cibles]
    if retires:
        _enregistrer_mots(raw, cle, [m for m in actuels if normaliser(m) not in cibles])
    return retires


def ecran_exclusions():
    modifie = False
    while True:
        raw = load_raw()
        entete()
        t = Table(title="[bold]4 · Tri et rangement › Mots à exclure du tri", title_justify="left", box=box.ROUNDED)
        t.add_column("#", style="bold cyan")
        t.add_column("Liste")
        t.add_column("Effet", style="dim", max_width=46)
        t.add_column("Vos mots")
        for i, (cle, (_, _, libelle, effet)) in enumerate(LISTES_EXCLUSION.items(), 1):
            mots = _mots(raw, cle)
            t.add_row(str(i), libelle, effet, ", ".join(mots) if mots else "[dim]—[/]")
        console.print(t)
        console.print("[dim]Un mot est reconnu sans tenir compte des majuscules ni des accents, et seulement en mot entier "
                      "(« edf » ne touche pas « fedf »). Plusieurs mots : séparez-les par des virgules.[/]")
        if modifie:
            console.print("[yellow]Listes modifiées : utilisez 6 pour les appliquer aux documents déjà rangés.[/]")
        choix = menu("Que voulez-vous faire ?", [
            ("1", "Ajouter des mots ignorés", "ex. le nom de votre banque dans votre signature"),
            ("2", "Ajouter des mots « à trier soi-même »", "ex. un nom de proche, un dossier personnel"),
            ("3", "Ajouter des mots confidentiels", "ex. un nom d'unité, de projet, de service"),
            ("4", "Retirer des mots", ""),
            ("5", "Tester un nom de fichier ou un objet de mail", "voir le rangement obtenu, rien n'est modifié"),
            ("6", "Appliquer à l'archive", "revérification, aperçu, puis re-rangement"),
        ])
        if choix == "0":
            return
        if choix in ("1", "2", "3"):
            cle = list(LISTES_EXCLUSION)[int(choix) - 1]
            nouveaux = saisir_mots(Prompt.ask(f"Mots à ajouter à « {LISTES_EXCLUSION[cle][2]} » (séparés par des virgules)"))
            courts = [m for m in nouveaux if len(m.replace(" ", "")) < 3]
            if courts and cle != "confidentiel" and not Confirm.ask(
                    f"{', '.join(courts)} : mot(s) très court(s), qui risquent de toucher beaucoup de documents. Garder ?", default=False):
                nouveaux = [m for m in nouveaux if m not in courts]
            ajout = ajouter_mots(raw, cle, nouveaux)
            if ajout:
                modifie = True
                succes("Mots ajoutés", ", ".join(ajout))
            else:
                info("Rien d'ajouté", "Ces mots étaient déjà dans la liste (ou la saisie était vide).")
            pause()
        elif choix == "4":
            options = [(str(i), LISTES_EXCLUSION[c][2], ", ".join(_mots(raw, c)) or "vide")
                       for i, c in enumerate(LISTES_EXCLUSION, 1)]
            n = menu("Retirer des mots de quelle liste ?", options, retour="Annuler")
            if n == "0":
                continue
            cle = list(LISTES_EXCLUSION)[int(n) - 1]
            retires = retirer_mots(raw, cle, saisir_mots(Prompt.ask("Mots à retirer (séparés par des virgules)")))
            if retires:
                modifie = True
                lignes = [", ".join(retires)]
                if cle == "confidentiel":
                    lignes.append("Par prudence, les documents déjà marqués confidentiels le restent. "
                                  "Les mots intégrés (gendarmerie, diplomatie…) ne peuvent pas être retirés.")
                succes("Mots retirés", lignes)
            else:
                info("Rien de retiré", "Aucun de ces mots n'était dans la liste.")
            pause()
        elif choix == "5":
            tester_exclusions()
        elif choix == "6":
            appliquer_exclusions()
            modifie = False


def tester_exclusions():
    from .confidentiel import Filtre
    from .themes import Classeur, arborescence

    phrase = Prompt.ask("Nom de fichier, objet de mail ou phrase à tester").strip()
    if not phrase:
        return
    raw = load_raw()
    sans = Classeur.charger()
    avec = Classeur.charger(exclusions=raw.get("exclusions"))
    conf = Filtre(raw.get("confidentiel")).texte(phrase)
    t = Table(box=box.ROUNDED, title=f"[bold]« {phrase} »", title_justify="left")
    t.add_column("")
    t.add_column("Sans vos mots exclus", style="dim")
    t.add_column("Avec vos mots exclus", style="bold")
    for libelle, c in (("sans", sans), ("avec", avec)):
        type_doc = c.classer_type(phrase, phrase, "", "Documents PDF").theme
        theme = c.classer(phrase, phrase, "", "", "Documents PDF").theme
        if libelle == "sans":
            r_sans = (type_doc, theme, "\\".join(arborescence(type_doc, theme, "AAAA")))
        else:
            r_avec = (type_doc, theme, "\\".join(arborescence(type_doc, theme, "AAAA", bool(conf))))
    for i, ligne in enumerate(("Type", "Domaine", "Rangé dans")):
        t.add_row(ligne, r_sans[i], r_avec[i])
    t.add_row("Confidentiel", "", f"oui ({conf}) : jamais envoyé à une IA en ligne" if conf else "non")
    console.print(t)
    console.print("[dim]Test sur le seul texte saisi (comme s'il était le nom du fichier et l'objet du mail) ; "
                  "le vrai tri tient aussi compte de l'expéditeur et du contenu.[/]")
    pause()


def appliquer_exclusions():
    from .reorganiser import reorganiser
    from .tri_assiste import reverifier

    cfg = load_config()
    try:
        with veille.verrou(cfg):
            with console.status("Revérification de la confidentialité (sur l'ordinateur uniquement)…"):
                n = reverifier(cfg)
            if n:
                console.print(f"[yellow]{nombre(n)} document(s) nouvellement marqué(s) confidentiel(s).[/]")
            with console.status("Simulation du rangement…"):
                reorganiser(cfg, simulation=True)
    except veille.Occupe as e:
        echec("Impossible pour l'instant", str(e))
        return pause()
    appliquer_rangement()


def apercu():
    from .reorganiser import reorganiser

    with console.status("Simulation du rangement…"):
        cfg = load_config()
    reorganiser(cfg, simulation=True)
    pause()


def appliquer_rangement():
    from .reorganiser import reorganiser

    if not Confirm.ask("Re-ranger toute l'archive avec les règles actuelles ? (les tris validés à la main sont conservés)", default=True):
        return
    cfg = load_config()
    try:
        with veille.verrou(cfg), console.status("Rangement en cours…"):
            journal = reorganiser(cfg)
        succes("Rangement appliqué", f"Journal des déplacements : {journal}")
    except veille.Occupe as e:
        echec("Rangement impossible pour l'instant", str(e))
    pause()


LIENS_AGENTS = [
    ("Claude Code (Anthropic)", "https://code.claude.com/docs/en/setup"),
    ("Gemini CLI (Google)", "https://github.com/google-gemini/gemini-cli"),
    ("Codex (OpenAI / ChatGPT)", "https://github.com/openai/codex"),
]
GUIDE_MEMOIRE = [
    ("Petit modèle (2 à 4 milliards de paramètres)", "4 à 6 Go"),
    ("Modèle moyen (7 à 9 milliards, ex. gemma4)", "10 à 12 Go"),
    ("Grand modèle (26 à 32 milliards)", "20 à 24 Go"),
]
SECONDES = {"rapide": 1.0, "precis": 10.0}  # mesuré avec gemma4 (8 Md) sur une RTX 5070 Ti 12 Go


def _duree(secondes: float) -> str:
    if secondes < 90:
        return f"≈ {max(1, round(secondes))} s"
    if secondes < 5400:
        return f"≈ {round(secondes / 60)} min"
    return f"≈ {secondes / 3600:.1f} h"


def ecran_ia():
    from . import ia_locale

    cfg = load_config()
    entete()
    with console.status("Analyse des documents restant à classer…"):
        tous = ia_locale.candidats(cfg)
        non_conf = [f for f in tous if not f["confidentiel"]]
        vram = ia_locale.memoire_carte_graphique_go()
    n = len(tous)
    if not n:
        succes("Rien à classer", "Tous les documents ont déjà un type et un domaine.")
        return pause()
    carte = f"{vram} Go".replace(".", ",") if vram else "non détectée"
    t = Table(title=f"[bold]Classer les {nombre(n)} documents restants avec une IA", title_justify="left", box=box.ROUNDED,
              show_lines=True)
    t.add_column("", style="bold")
    t.add_column("1 · Agent IA en ligne", ratio=1, overflow="fold")
    t.add_column("2 · IA locale, hors ligne", ratio=1, overflow="fold")
    t.add_row("Exemples", "Claude Code, Gemini CLI, Codex (ChatGPT)…", "Ollama + un modèle (Gemma 4, Qwen…)")
    t.add_row("Prérequis", "[yellow]Agent déjà installé[/] sur ce PC et abonnement actif.\nL'outil ne l'installe pas.",
              "[yellow]Ollama et un modèle déjà installés[/].\nL'outil ne les installe pas.")
    t.add_row("Internet", "Oui : les extraits des documents sont envoyés au fournisseur.",
              "[green]Non : rien ne quitte l'ordinateur.[/]")
    t.add_row("Documents traités", f"Jusqu'à {nombre(len(non_conf))} : jamais les confidentiels (gendarmerie, diplomatie), "
              "ni ceux échangés uniquement entre particuliers, ni les scans.",
              f"Les {nombre(n)}, [green]confidentiels compris[/]. Scans et photos seulement si le modèle sait lire les images "
              "(vérifié automatiquement).")
    t.add_row("Mémoire de carte graphique", "Aucune : le calcul se fait chez le fournisseur.",
              "\n".join(f"{a} : {b}" for a, b in GUIDE_MEMOIRE) + f"\n[bold]Votre carte : {carte}[/]")
    t.add_row("Durée estimée", "Quelques minutes pour une centaine de documents (selon l'agent), puis votre validation.",
              f"Rapide : {_duree(n * SECONDES['rapide'])} (≈ 1 s/document)\nPrécis : {_duree(n * SECONDES['precis'])} "
              "(≈ 10 s/document)\n[dim]mesuré avec gemma4 sur une carte de 12 Go ; nettement plus lent sans carte graphique suffisante[/]")
    t.add_row("Se renseigner", "Liens ci-dessous (1).", "Liens ci-dessous (2).")
    console.print(t)
    console.print("[bold]Pour vous renseigner sur l'installation[/] (l'outil n'installe rien lui-même) :")
    for nom, lien in LIENS_AGENTS:
        console.print(f"  (1) {nom} : [cyan]{lien}[/]", soft_wrap=True)
    for nom, lien in ia_locale.LIENS.items():
        console.print(f"  (2) {nom} : [cyan]{lien}[/]", soft_wrap=True)
    console.print("[dim]Dans les deux cas : aperçu et validation avant tout déplacement, journal, annulation possible (choix 7).[/]")
    choix = Prompt.ask("[bold]Votre choix[/] (1, 2 ou 0 pour revenir)", choices=["1", "2", "0"], show_choices=False)
    if choix == "1":
        ecran_agent()
    elif choix == "2":
        ecran_local(tous)


def ecran_agent():
    from .tri_assiste import appliquer, dossier_tri, exporter

    while True:
        cfg = load_config()
        entete()
        info("1 · Agent IA en ligne", [
            "[yellow]Prérequis : un agent IA déjà installé sur ce PC, capable de lire et d'écrire des fichiers.[/]",
            "L'outil ne l'installe pas. Pour vous renseigner :",
            *[f"  • {nom} : {lien}" for nom, lien in LIENS_AGENTS],
            "", "Mémoire de carte graphique : aucune. Internet : oui.",
            "Confidentialité : l'export exclut automatiquement les documents confidentiels, ceux échangés uniquement "
            "entre particuliers et ceux sans texte lisible."])
        choix = menu("Étapes", [
            ("1", "Préparer les documents pour votre agent", "export filtré + consigne prête à l'emploi"),
            ("2", "Appliquer les décisions de votre agent", "après votre vérification"),
        ])
        if choix == "0":
            return
        if choix == "1":
            with console.status("Vérification de confidentialité et préparation de l'export…"):
                export = exporter(cfg)
            d = export.parent
            info("Export prêt", [
                f"1. Vérifiez la liste : [bold]{d / 'A_VERIFIER_avant_envoi_a_Claude.txt'}[/]",
                "2. Ouvrez votre agent IA et tapez :",
                f"   [bold cyan]Lis le fichier {d / 'CONSIGNE_pour_agent_IA.txt'} et suis ses instructions.[/]",
                f"3. L'agent écrit ses choix dans [bold]{d / 'decisions.json'}[/] ; revenez ici, étape 2."])
            pause()
        else:
            f = dossier_tri(cfg) / "decisions.json"
            if not f.exists():
                echec("Aucune décision trouvée", f"Le fichier {f} n'existe pas encore : faites d'abord travailler votre agent.")
                pause()
                continue
            try:
                decisions = json.loads(f.read_text(encoding="utf-8")).get("decisions", [])
            except (json.JSONDecodeError, AttributeError) as e:
                echec("Fichier de décisions illisible", str(e))
                pause()
                continue
            t = Table(title=f"[bold]{len(decisions)} décision(s) proposée(s) par votre agent", title_justify="left", box=box.ROUNDED)
            t.add_column("Type")
            t.add_column("Documents", justify="right")
            from collections import Counter

            for typ, nb in Counter(x.get("type", "?") for x in decisions).most_common():
                t.add_row(typ, str(nb))
            console.print(t)
            if Confirm.ask("Appliquer ces décisions ? (les documents confidentiels sont refusés d'office)", default=True):
                appliquer(cfg, f)
                f.rename(f.with_name(f"decisions_appliquees_{datetime.now():%Y-%m-%d_%H%M%S}.json"))
                succes("Décisions appliquées", "Annulation possible : 4 · Tri et rangement → 8.")
            pause()


def ecran_local(candidats: list):
    from . import ia_locale

    entete()
    info("2 · IA locale, hors ligne", [
        "[yellow]Prérequis : Ollama et au moins un modèle déjà installés.[/] L'outil ne les installe pas. Pour vous renseigner :",
        *[f"  • {nom} : {lien}" for nom, lien in ia_locale.LIENS.items()],
        "", "[green]Aucun accès Internet : seule l'IA de cet ordinateur (127.0.0.1) est contactée.[/]",
        "Mémoire de carte graphique nécessaire : " + " · ".join(f"{a.split(' (')[0].lower()} {b}" for a, b in GUIDE_MEMOIRE)])
    try:
        with console.status("Démarrage de l'IA locale…"):
            version = ia_locale.demarrer()
            modeles = ia_locale.modeles()
            vram = ia_locale.memoire_carte_graphique_go()
    except ia_locale.IALocaleIndisponible as e:
        echec("IA locale indisponible", f"{e}\nRenseignez-vous sur l'installation : {ia_locale.LIENS['Ollama (installation)']}")
        return pause()
    if not modeles:
        echec("Aucun modèle installé", f"Ollama {version} est présent, mais sans modèle. Liste des modèles : {ia_locale.LIENS['Modèles disponibles']}")
        return pause()
    t = Table(title=f"[bold]Modèles installés[/]  [dim]Ollama {version} · carte graphique : {str(vram or '?').replace('.', ',')} Go", title_justify="left",
              box=box.ROUNDED)
    for col in ("#", "Modèle", "Paramètres", "Mémoire nécessaire", "Sur votre carte"):
        t.add_column(col)
    for i, m in enumerate(modeles, 1):
        tient = "?" if vram is None else ("[green]✓ tient[/]" if m.memoire_necessaire_go <= vram else "[yellow]⚠ trop gros : lent[/]")
        params = m.parametres.replace("B", " Md").replace(".0 ", " ").replace(".", ",")
        t.add_row(str(i), m.nom, params, f"≈ {m.memoire_necessaire_go} Go".replace(".", ","), tient)
    console.print(t)
    r = ia_locale.reglages()
    defaut = next((str(i) for i, m in enumerate(modeles, 1) if m.nom == r.get("modele")), "1")
    modele = modeles[int(Prompt.ask("Modèle", choices=[str(i) for i in range(1, len(modeles) + 1)], default=defaut)) - 1]
    n = len(candidats)
    console.print(f"\n  [bold cyan]1[/]  Rapide  — {_duree(n * SECONDES['rapide'])} pour {nombre(n)} documents (≈ 9 bons sur 10 lors de nos essais)")
    console.print(f"  [bold cyan]2[/]  Précis  — {_duree(n * SECONDES['precis'])} pour {nombre(n)} documents (≈ 10 sur 10 ; l'IA « réfléchit » avant de répondre)")
    reflexion = Prompt.ask("Mode", choices=["1", "2"], default="2" if r.get("reflexion") else "1") == "2"
    if not Confirm.ask("Lancer le classement ? (rien n'est déplacé avant votre validation)", default=True):
        return
    rap = RichRapporteur()
    ancien = progression.utiliser(rap)
    try:
        with console.status("Vérification : le modèle sait-il lire les images ?"):
            vision = ia_locale.tester_vision(modele)
        res = ia_locale.proposer(load_config(), modele, vision=vision, reflexion=reflexion)
    except KeyboardInterrupt:
        echec("Classement interrompu", "Rien n'a été déplacé.")
        return pause()
    finally:
        rap.fin_dossier()
        progression.utiliser(ancien)
    seuil = float(r.get("seuil", ia_locale.SEUIL_DEFAUT))
    choisies = ia_locale.utiles(res["propositions"], seuil)
    t = Table(title=f"[bold]{len(choisies)} classement(s) proposé(s)[/]  [dim](confiance ≥ {seuil:.0%})", title_justify="left",
              box=box.ROUNDED)
    for col in ("Fichier", "Type", "Domaine", "Confiance"):
        t.add_column(col, overflow="fold")
    for p in choisies[:80]:
        t.add_row(("🔒 " if p["confidentiel"] else "") + p["fichier"], p["type"], p["domaine"].replace("/", " › "), f"{p['confiance']:.0%}")
    console.print(t)
    if len(choisies) > 80:
        console.print(f"[dim]… et {len(choisies) - 80} autres.[/]")
    console.print(f"[dim]{res['illisibles']} document(s) sans texte lisible "
                  f"({'images lues' if res['vision'] else 'ce modèle ne lit pas les images'}) · "
                  f"{len(res['propositions']) - len(choisies)} restent « À classer » (IA pas assez sûre) · durée {_duree(res['duree_s'])}[/]")
    if choisies and Confirm.ask(f"Appliquer ces {len(choisies)} classement(s) ?", default=True):
        ia_locale.appliquer(load_config(), res["propositions"], seuil)
        succes("Classement appliqué — sans aucun accès Internet", "Annulation possible : 4 · Tri et rangement → 8.")
    r.update({"modele": modele.nom, "reflexion": reflexion})
    r["dans_veille"] = Confirm.ask("Classer aussi automatiquement les nouveaux documents à chaque passage de la veille ?",
                                   default=bool(r.get("dans_veille")))
    ia_locale.enregistrer_reglages(r)
    pause()


def annuler_tri():
    from .tri_assiste import annuler, dossier_tri

    cfg = load_config()
    journaux = sorted(dossier_tri(cfg).glob("journal_*.csv")) if dossier_tri(cfg).exists() else []
    if not journaux:
        echec("Rien à annuler", "Aucun tri assisté n'a été appliqué.")
        return pause()
    if Confirm.ask(f"Annuler le tri du {journaux[-1].stem.removeprefix('journal_')} ?", default=False):
        annuler(cfg, journaux[-1])
        succes("Tri annulé", "Les fichiers sont revenus à leur place précédente.")
    pause()


# ───────────────────────── 5. veille ─────────────────────────

def ecran_veille():
    while True:
        cfg = load_config()
        entete()
        v = veille.reglages()
        e = veille.etat()
        lignes = [f"État : [green]active[/] — {veille.description(v)}" if e is not None else "État : [dim]désactivée[/]"]
        if e:
            lignes += [f"Prochaine exécution : {e.get('prochaine') or '?'}", f"Dernière exécution : {e.get('derniere') or 'jamais'}"]
        journal = veille.dernieres_lignes(cfg, 5)
        if journal:
            lignes += ["", "[bold]Derniers passages :[/]", *[f"[dim]{l}[/]" for l in journal]]
        info("5 · Veille automatique", lignes + ["", "[dim]À chaque passage : scan rapide (complet une fois par mois) puis récupération des nouveaux mails, "
                                                      "sans fenêtre, en lecture seule. Fonctionne sur batterie ; un passage manqué "
                                                      "(PC éteint) est rattrapé au démarrage.[/]"])
        choix = menu("Que voulez-vous faire ?", [
            ("1", "Activer / modifier la planification", ""),
            ("2", "Lancer un passage maintenant", "pour tester"),
            ("3", "Désactiver la veille", "" if e is not None else "🔒 déjà désactivée"),
            ("4", "Ouvrir le journal de la veille", ""),
        ])
        if choix == "0":
            return
        if choix == "1":
            planifier(v)
        elif choix == "2":
            with console.status("Passage de veille en cours (scan puis récupération)…"):
                code = veille.executer(cfg)
            derniere = veille.dernieres_lignes(cfg, 1)
            (succes if code == 0 else echec)("Passage terminé" if code == 0 else "Passage en échec", derniere[0] if derniere else "")
            pause()
        elif choix == "3" and e is not None:
            veille.desactiver()
            succes("Veille désactivée", "Plus aucun passage automatique.")
            pause()
        elif choix == "4":
            j = veille.journal(cfg)
            if j.exists():
                subprocess.Popen(["notepad.exe", str(j)])
            else:
                console.print("[dim]Aucun passage pour l'instant.")
                pause()


def planifier(v: dict):
    f = menu("Fréquence", [("1", "Toutes les heures", ""), ("2", "Toutes les 6 heures", ""), ("3", "Chaque jour", "à l'heure choisie"),
                           ("4", "Chaque semaine", "jour et heure choisis")], retour="Annuler")
    if f == "0":
        return
    v["frequence"] = {"1": "horaire", "2": "horaire", "3": "quotidienne", "4": "hebdomadaire"}[f]
    v["intervalle_h"] = 1 if f == "1" else 6
    if f in ("3", "4"):
        while True:
            h = Prompt.ask("Heure (HH:MM)", default=v.get("heure", "08:00"))
            try:
                v["heure"] = datetime.strptime(h.strip(), "%H:%M").strftime("%H:%M")
                break
            except ValueError:
                console.print("[red]Format attendu : 08:00")
    if f == "4":
        v["jour"] = Prompt.ask("Jour", choices=list(veille.JOURS), default=v.get("jour", "lundi"))
    m = Prompt.ask("Mode : [bold]1[/] mail complet + pièces jointes, [bold]2[/] pièces jointes uniquement", choices=["1", "2"],
                   default="1" if v.get("mode", "complet") == "complet" else "2")
    v["mode"] = "complet" if m == "1" else "pj"
    ok, message = veille.activer(v)
    (succes if ok else echec)("Veille programmée" if ok else "Programmation impossible", message)
    pause()


# ───────────────────────── 6. recherche ─────────────────────────

def ecran_recherche():
    cfg = load_config()
    entete()
    q = Prompt.ask("[bold]Rechercher[/] (ex. « facture edf 2024 », « quittance », « assurance vie »)").strip()
    if not q:
        return
    res = recherche.rechercher(cfg, q)
    if not res:
        echec("Aucun résultat", f"Rien ne correspond à « {q} » (nom du fichier, objet du mail, correspondant, type, domaine, année).")
        return pause()
    t = Table(title=f"[bold]{len(res)} résultat(s) pour « {q} »", title_justify="left", box=box.ROUNDED)
    for col in ("#", "Date", "Type", "Domaine", "Fichier", "Correspondant"):
        t.add_column(col, overflow="fold")
    for i, r in enumerate(res, 1):
        t.add_row(str(i), r["date_mail"] or "", r["type_doc"] or "", (r["theme"] or "").replace("/", " › "),
                  r["nom_origine"] or Path(r["chemin_local"]).name, r["correspondant"] or "")
    console.print(t)
    n = Prompt.ask("Numéro à afficher dans l'Explorateur (Entrée pour revenir)", default="", show_default=False)
    if n.isdigit() and 1 <= int(n) <= len(res):
        recherche.montrer_dans_explorateur(res[int(n) - 1]["chemin_local"])


# ───────────────────────── accueil ─────────────────────────

DUREE_PREMIERE_RECUPERATION = (
    "La [bold]première récupération[/] (téléchargement et enregistrement des pièces jointes) dépend à la fois de "
    "votre [bold]connexion internet[/] et du [bold]volume de vos boîtes mail[/] : comptez en général "
    "[bold]entre 30 minutes et 2 heures[/]. Les suivantes ne prennent que quelques minutes. Vous pouvez utiliser "
    "votre ordinateur pendant ce temps, et l'interrompre (Ctrl+C) : la reprise saute ce qui est déjà fait.")


def premiere_recuperation(cfg) -> bool:
    conn = db.ouvrir(cfg.base_index)
    n = conn.execute("SELECT COUNT(*) FROM mails_recuperes").fetchone()[0]
    conn.close()
    return n == 0


def choisir_emplacement_initial():
    """Premier lancement : l'archive va sur le Bureau, sauf si vous choisissez un autre emplacement."""
    bureau, documents = desktop_dir() / "Archive mail", dossier_documents() / "Archive mail"
    info("Où ranger votre archive ?", [
        f"Par défaut, elle est créée sur le [bold]Bureau[/] : {bureau}",
        "Vous pouvez aussi choisir un autre emplacement (et le changer plus tard : 4 · Tri et rangement).",
        "", f"  [bold cyan]1[/]  Bureau  [green](par défaut)[/]", f"  [bold cyan]2[/]  Documents  [dim]{documents}[/]",
        "  [bold cyan]3[/]  Autre emplacement (disque externe, NAS…)"])
    while True:
        choix = Prompt.ask("[bold]Votre choix", choices=["1", "2", "3"], default="1", show_choices=False)
        cible = {"1": bureau, "2": documents}.get(choix)
        if cible is None:
            saisie = Prompt.ask("Chemin complet du dossier (ex. E:\\Archive mail)").strip().strip('"')
            if not saisie:
                continue
            cible = Path(os.path.expandvars(saisie)).expanduser()
        try:
            cible.mkdir(parents=True, exist_ok=True)
            essai = cible / ".test_ecriture"
            essai.write_text("ok", encoding="utf-8")
            essai.unlink()
        except OSError as e:
            echec("Emplacement inutilisable", f"{cible} : {e}. Choisissez-en un autre.")
            continue
        raw = load_raw()
        raw["dossier_archive"] = "{BUREAU}/Archive mail" if choix == "1" else str(cible.resolve())
        raw["emplacement_choisi"] = True
        save_raw(raw)
        succes("Emplacement enregistré", f"Votre archive sera rangée dans : [bold]{cible}[/]")
        return


def accueil() -> int:
    while True:
        cfg = load_config()
        entete()
        if not cfg.comptes:
            info("Bienvenue", ["Première utilisation, en trois étapes :",
                               "  1. choisir où ranger l'archive (le Bureau par défaut) ;",
                               "  2. paramétrer vos boîtes mail (la messagerie est reconnue automatiquement) ;",
                               "  3. lancer le scan à blanc, puis la récupération.",
                               "", DUREE_PREMIERE_RECUPERATION])
            if not load_raw().get("emplacement_choisi") and not cfg.base_index.exists():
                choisir_emplacement_initial()
            ajouter_boite()
            if not load_config().comptes and not Confirm.ask("Réessayer ?", default=True):
                return 0
            continue
        console.print(etat(cfg))
        s = dernier_scan(cfg)
        choix = menu("Menu principal", [
            ("1", "Paramétrer les boîtes mail", "ajouter, tester, choisir les dossiers"),
            ("2", "Scan à blanc", "rapide (nouveaux mails) ou complet · rien n'est téléchargé"),
            ("3", "Récupération", "mail complet + PJ, ou PJ uniquement" if s else "🔒 faites d'abord le scan (2)"),
            ("4", "Tri et rangement", "emplacement, règles, tri assisté"),
            ("5", "Veille automatique", "scan + récupération programmés"),
            ("6", "Rechercher un document", "par nom, objet, correspondant, année…"),
        ], retour="Quitter")
        if choix == "0":
            console.print("[dim]À bientôt.[/]")
            return 0
        try:
            if choix == "1":
                ecran_boites()
            elif choix == "2":
                ecran_scan(cfg)
            elif choix == "3":
                ecran_recuperation(cfg)
            elif choix == "4":
                ecran_tri()
            elif choix == "5":
                ecran_veille()
            elif choix == "6":
                ecran_recherche()
        except KeyboardInterrupt:
            console.print("\n[yellow]Opération annulée.[/]")
            pause()
