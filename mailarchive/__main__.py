"""Point d'entrée : python -m mailarchive [commande]

  (aucune)       menu interactif
  compte         ajouter une adresse (détection de la messagerie, test, enregistrement)
  scan           scan à blanc : inventaire et estimation, sans rien télécharger
  recuperer      récupération des mails et pièces jointes, puis classement
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as cfgmod


def cmd_menu(args) -> int:
    from .tui import accueil

    return accueil()


def cmd_compte(args) -> int:
    from .comptes import enregistrer

    password = Path(args.depuis_fichier).read_text(encoding="utf-8").strip() if args.depuis_fichier else None
    return 0 if enregistrer(args.email, password=password, hote=args.serveur) else 1


def cmd_scan(args) -> int:
    from .scan import lancer

    lancer(cfgmod.load_config(), args.compte, args.limite, ouvrir=not args.sans_ouvrir, rapide=args.rapide)
    return 0


def cmd_recuperer(args) -> int:
    from .recuperation import lancer

    return 0 if lancer(cfgmod.load_config(), ouvrir=not args.sans_ouvrir, comptes_filtre=args.compte) else 1


def cmd_tri_export(args) -> int:
    from .tri_assiste import exporter

    exporter(cfgmod.load_config())
    return 0


def cmd_tri_appliquer(args) -> int:
    from .tri_assiste import appliquer

    return appliquer(cfgmod.load_config(), Path(args.decisions))


def cmd_tri_annuler(args) -> int:
    from .tri_assiste import annuler

    return annuler(cfgmod.load_config(), Path(args.journal))


def cmd_reorganiser(args) -> int:
    from .reorganiser import reorganiser

    reorganiser(cfgmod.load_config(), simulation=args.simulation)
    return 0


def main(argv=None) -> int:
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(prog="mailarchive", description="Archivage des pièces jointes de vos boîtes mail.")
    sub = p.add_subparsers(dest="commande")

    sub.add_parser("menu", help="interface interactive (par défaut)").set_defaults(fn=cmd_menu)
    sub.add_parser("veille", help="un passage de veille : scan puis récupération (utilisé par la tâche planifiée)").set_defaults(
        fn=lambda a: __import__("mailarchive.veille", fromlist=["executer"]).executer())

    pc = sub.add_parser("compte", help="ajouter une adresse mail")
    pc.add_argument("email")
    pc.add_argument("--depuis-fichier", help="lire le mot de passe dans ce fichier au lieu de le demander")
    pc.add_argument("--serveur", help="serveur IMAP si la messagerie n'est pas reconnue")
    pc.set_defaults(fn=cmd_compte)

    ps = sub.add_parser("scan", help="scan à blanc (aucun téléchargement)")
    ps.add_argument("--compte", action="append", help="limiter à ce compte ; répétable")
    ps.add_argument("--limite", type=int, help="ne lire que les N derniers mails de chaque dossier (test rapide)")
    ps.add_argument("--sans-ouvrir", action="store_true", help="ne pas ouvrir le rapport à la fin")
    ps.add_argument("--rapide", action="store_true", help="n'analyser que les mails arrivés depuis le dernier scan")
    ps.set_defaults(fn=cmd_scan)

    pr = sub.add_parser("recuperer", help="récupérer et classer (nécessite un scan à blanc complet)")
    pr.add_argument("--compte", action="append", help="limiter à ce compte ; répétable")
    pr.add_argument("--sans-ouvrir", action="store_true", help="ne pas ouvrir le rapport à la fin")
    pr.set_defaults(fn=cmd_recuperer)

    pe = sub.add_parser("tri-export", help="préparer l'export filtré du dossier « À classer » pour le tri assisté")
    pe.set_defaults(fn=cmd_tri_export)
    pa = sub.add_parser("tri-appliquer", help="appliquer les décisions de tri validées")
    pa.add_argument("decisions")
    pa.set_defaults(fn=cmd_tri_appliquer)
    pu = sub.add_parser("tri-annuler", help="annuler un tri appliqué (journal_*.csv)")
    pu.add_argument("journal")
    pu.set_defaults(fn=cmd_tri_annuler)

    po = sub.add_parser("reorganiser", help="ranger l'archive par type de document (Type/Domaine/Année)")
    po.add_argument("--simulation", action="store_true", help="afficher le résultat sans rien déplacer")
    po.set_defaults(fn=cmd_reorganiser)

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        return cmd_menu(args)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
