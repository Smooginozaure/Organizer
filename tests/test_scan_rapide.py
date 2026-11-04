import shutil
import tempfile
import unittest
from pathlib import Path

from mailarchive import credentials, db, scan
from mailarchive.config import Compte, Config, RECUPERATION_DEFAUT
from mailarchive.imap_client import Dossier


def reponse(uid: int, piece: str | None) -> bytes:
    if piece:
        corps = (f'(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 10 1 NIL NIL NIL NIL)("APPLICATION" "PDF" ("NAME" "{piece}") NIL NIL '
                 f'"BASE64" 40000 NIL ("ATTACHMENT" ("FILENAME" "{piece}")) NIL NIL) "MIXED" NIL NIL NIL NIL)')
    else:
        corps = '("TEXT" "PLAIN" NIL NIL NIL "7BIT" 10 1 NIL NIL NIL NIL)'
    return (f'1 (UID {uid} RFC822.SIZE 50000 INTERNALDATE "05-Mar-2024 10:12:00 +0100" ENVELOPE '
            f'(NIL "Objet {uid}" (("EDF" NIL "facture" "edf.fr")) NIL NIL NIL NIL NIL NIL NIL) BODYSTRUCTURE {corps})').encode()


class FakeServeur:
    """Une boîte avec un seul dossier ; on peut ajouter/supprimer des mails ou renuméroter le dossier."""
    mails: dict[int, str | None] = {}
    uidvalidity = "1"
    demandes: list[list[str]] = []

    def __init__(self, *a, **k):
        self.uidvalidity = ""

    def connect(self):
        pass

    def close(self):
        pass

    def dossiers(self):
        return [Dossier("INBOX", "INBOX", [])]

    def ouvrir(self, d):
        self.uidvalidity = FakeServeur.uidvalidity
        return len(FakeServeur.mails)

    def uids(self):
        return [str(u) for u in sorted(FakeServeur.mails)]

    def fetch(self, uids, items):
        FakeServeur.demandes.append(list(uids))
        return [reponse(int(u), FakeServeur.mails[int(u)]) for u in uids]


class ScanRapide(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(dossier_archive=self.tmp, comptes=[Compte("Apple", "icloud", "moi@icloud.com", "h", 993)],
                          recuperation=dict(RECUPERATION_DEFAUT))
        self._orig = (scan.Session, credentials.get_password)
        scan.Session = FakeServeur
        credentials.get_password = lambda c: "x"
        FakeServeur.mails = {42: "f.pdf", 43: None}
        FakeServeur.uidvalidity = "1"
        FakeServeur.demandes = []

    def tearDown(self):
        scan.Session, credentials.get_password = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _inventaire(self):
        conn = db.ouvrir(self.cfg.base_index)
        s = db.dernier_scan_complet(conn)
        uids = sorted(r[0] for r in conn.execute("SELECT uid FROM messages WHERE scan_id = ?", (s["id"],)))
        pj = sorted(r[0] for r in conn.execute("SELECT nom FROM pieces WHERE scan_id = ?", (s["id"],)))
        conn.close()
        return s, uids, pj

    def test_premier_scan_rapide_lit_tout(self):
        scan.lancer(self.cfg, ouvrir=False, afficher_resume=False, rapide=True)
        s, uids, _ = self._inventaire()
        self.assertEqual((s["mode"], uids), ("complet", [42, 43]))

    def test_seuls_les_nouveaux_sont_analyses(self):
        scan.lancer(self.cfg, ouvrir=False, afficher_resume=False)
        FakeServeur.demandes = []
        FakeServeur.mails = {43: None, 44: "g.pdf", 45: None}  # 42 supprimé, 44 et 45 arrivés
        dossier = scan.lancer(self.cfg, ouvrir=False, afficher_resume=False, rapide=True)

        self.assertEqual(FakeServeur.demandes, [["44", "45"]])  # le serveur n'est interrogé que pour les nouveaux
        s, uids, pj = self._inventaire()
        self.assertEqual((s["mode"], s["nouveaux"], s["supprimes"]), ("incremental", 2, 1))
        self.assertEqual(uids, [43, 44, 45])
        self.assertEqual(pj, ["g.pdf"])
        self.assertIn("Scan rapide", (dossier / "rapport.txt").read_text(encoding="utf-8-sig"))

        # Un second scan rapide sans nouveauté ne demande rien au serveur et garde l'inventaire.
        FakeServeur.demandes = []
        scan.lancer(self.cfg, ouvrir=False, afficher_resume=False, rapide=True)
        self.assertEqual(FakeServeur.demandes, [])
        self.assertEqual(self._inventaire()[1:], ([43, 44, 45], ["g.pdf"]))

    def test_dossier_renumerote_relu_entierement(self):
        scan.lancer(self.cfg, ouvrir=False, afficher_resume=False)
        FakeServeur.demandes = []
        FakeServeur.uidvalidity = "2"
        scan.lancer(self.cfg, ouvrir=False, afficher_resume=False, rapide=True)
        self.assertEqual(FakeServeur.demandes, [["42", "43"]])
        self.assertEqual(self._inventaire()[1:], ([42, 43], ["f.pdf"]))

    def test_estimation_identique_en_rapide_et_complet(self):
        scan.lancer(self.cfg, ouvrir=False, afficher_resume=False)
        FakeServeur.mails[44] = "g.pdf"
        rapide = scan.lancer(self.cfg, ouvrir=False, afficher_resume=False, rapide=True)
        complet = scan.lancer(self.cfg, ouvrir=False, afficher_resume=False)
        import json
        a = json.loads((rapide / "resume.json").read_text(encoding="utf-8"))
        b = json.loads((complet / "resume.json").read_text(encoding="utf-8"))
        for cle in ("mails", "volume_boites_octets", "pj_utiles_octets", "recuperation_pj_octets", "recuperation_mails_octets"):
            self.assertEqual(a[cle], b[cle], cle)


if __name__ == "__main__":
    unittest.main()
