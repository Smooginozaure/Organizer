import email.policy
import shutil
import tempfile
import unittest
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from mailarchive import credentials, db, recuperation
from mailarchive.config import Compte, Config, RECUPERATION_DEFAUT
from mailarchive.imap_client import OperationInterdite, verifier_commande
from mailarchive.themes import Classeur


class LectureSeule(unittest.TestCase):
    def test_commandes_dangereuses_refusees(self):
        for name, args in [
            ("STORE", ("1", "+FLAGS", "(\\Deleted)")),
            ("EXPUNGE", ()),
            ("CLOSE", ()),
            ("DELETE", ("INBOX",)),
            ("MOVE", ("1", "Trash")),
            ("COPY", ("1", "Trash")),
            ("APPEND", ("INBOX",)),
            ("SELECT", ("INBOX",)),  # seul EXAMINE (lecture seule) est permis
            ("UID", ("STORE", "1", "+FLAGS", "(\\Seen)")),
            ("UID", ("EXPUNGE", "1")),
            ("UID", ("MOVE", "1", "Trash")),
            ("UID", ("FETCH", "1", "(BODY[])")),  # marquerait comme lu
            ("UID", ("FETCH", "1", "(RFC822)")),
        ]:
            with self.subTest(name=name, args=args), self.assertRaises(OperationInterdite):
                verifier_commande(name, args)

    def test_lectures_autorisees(self):
        for name, args in [
            ("EXAMINE", ('"INBOX"',)),
            ("LIST", ('""', '"*"')),
            ("UID", ("SEARCH", None, "ALL")),
            ("UID", ("FETCH", "1:5", "(UID RFC822.SIZE INTERNALDATE ENVELOPE BODYSTRUCTURE)")),
            ("UID", ("FETCH", "1,2", "(UID BODY.PEEK[])")),
            ("LOGOUT", ()),
        ]:
            with self.subTest(name=name):
                verifier_commande(name, args)


class Themes(unittest.TestCase):
    def setUp(self):
        self.c = Classeur.charger()

    def t(self, nom, objet="", adresse="", contenu="", cat="PDF"):
        return self.c.classer(nom, objet, adresse, contenu, cat).theme

    def test_exemples(self):
        self.assertEqual(self.t("Quittance_loyer_mars.pdf"), "Logement/Location")
        self.assertEqual(self.t("doc.pdf", "Votre contrat d'assurance vie"), "Assurances/Assurance vie")
        self.assertEqual(self.t("facture.pdf", "Votre facture", "noreply@edf.fr"), "Énergie/Électricité - Gaz")
        self.assertEqual(self.t("Avis_impot_2024.pdf", contenu="Avis d'imposition sur le revenu"), "Impôts")
        self.assertEqual(self.t("scan.pdf", contenu="BULLETIN DE PAIE net à payer"), "Travail/Bulletins de salaire")

    def test_secours(self):
        self.assertEqual(self.t("IMG_0001.jpeg", cat="Images / photos"), "Photos et vidéos")
        self.assertEqual(self.t("document.pdf"), "À classer")
        self.assertEqual(self.t("setup.exe", cat="Exécutables / scripts (quarantaine)"), "_Quarantaine (ne pas ouvrir)")

    def test_adresse_perso_free_non_classee_telecom(self):
        self.assertEqual(self.t("photo.pdf", "coucou", "moi@free.fr"), "À classer")


def _mail() -> bytes:
    m = EmailMessage()
    m["From"] = "Agence Dupont <agence@immo-dupont.fr>"
    m["To"] = "moi@icloud.com"
    m["Subject"] = "Votre quittance de loyer"
    m.set_content("Bonjour, ci-joint votre quittance.")
    m.add_alternative("<p>Bonjour <img src='cid:logo'></p>", subtype="html")
    m.get_payload()[1].add_related(b"\x89PNG" + b"0" * 2000, "image", "png", cid="<logo>", filename="logo.png")
    m.add_attachment(b"%PDF-1.4 contenu", maintype="application", subtype="pdf", filename="Quittance mars 2024.pdf")
    fwd = EmailMessage()
    fwd["Subject"] = "transfert"
    fwd.set_content("mail transféré")
    m.add_attachment(fwd)
    return m.as_bytes(policy=email.policy.SMTP)


class FakeSession:
    raw = _mail()

    def __init__(self, compte, password, timeout=120):
        self.uidvalidity = ""

    def connect(self):
        pass

    def close(self):
        pass

    def ouvrir(self, dossier):
        self.uidvalidity = "777"
        return 1

    def fetch(self, uids, items):
        assert "BODY.PEEK[]" in items
        out = []
        for u in uids:
            if u == "42":
                out += [(f"1 (UID 42 BODY[] {{{len(self.raw)}}}".encode(), self.raw), b")"]
        return out  # le mail 43 a « disparu » du serveur depuis le scan


class Reorganisation(unittest.TestCase):
    """Ancienne archive rangée par boîte -> arborescence par type ; doublon entre boîtes fusionné."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(dossier_archive=self.tmp, comptes=[Compte("Apple", "icloud", "moi@icloud.com", "h", 993),
                                                             Compte("Free", "free", "moi@free.fr", "h", 993)],
                          recuperation=dict(RECUPERATION_DEFAUT))
        import hashlib
        contenu = b"Quittance de loyer du mois de mars"
        self.sha = hashlib.sha256(contenu).hexdigest()
        a = self.tmp / "Apple" / "Logement" / "Location" / "2024" / "2024-03-05 - Agence - Quittance mars.txt"
        f = self.tmp / "Free" / "À classer" / "2024" / "2024-03-05 - Agence - Quittance mars.txt"
        e = self.tmp / "Apple" / "_Mails sauvegardés" / "Boîte de réception" / "2024" / "m.eml"
        for chemin, data in ((a, contenu), (f, contenu), (e, b"From: agence@immo.fr\r\nTo: moi@icloud.com\r\n\r\nx")):
            chemin.parent.mkdir(parents=True, exist_ok=True)
            chemin.write_bytes(data)
        conn = db.ouvrir(self.cfg.base_index)
        with conn:
            conn.execute("INSERT INTO fichiers (compte, sha256, chemin_local, theme, taille) VALUES ('Apple', ?, ?, 'Logement/Location', 10)", (self.sha, str(a)))
            conn.execute("INSERT INTO fichiers (compte, sha256, chemin_local, theme, taille) VALUES ('Free', ?, ?, 'À classer', 10)", (self.sha, str(f)))
            for compte, chemin in (("Apple", a), ("Free", f)):
                conn.execute("INSERT INTO pieces_recuperees (compte, dossier, uidvalidity, uid, chemin, nom_origine, sha256, chemin_local, "
                             "theme, doublon, date_mail, correspondant, objet) VALUES (?, 'INBOX', '1', 1, '2', 'Quittance mars.txt', ?, ?, "
                             "'x', 0, '2024-03-05', 'Agence', 'Votre quittance')", (compte, self.sha, str(chemin)))
            conn.execute("INSERT INTO mails_recuperes VALUES ('Apple', 'INBOX', '1', 1, ?, '2026-11-03')", (str(e),))
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reorganiser(self):
        from mailarchive.reorganiser import reorganiser
        reorganiser(self.cfg, simulation=True)
        self.assertTrue((self.tmp / "Free" / "À classer" / "2024").exists())  # la simulation ne touche à rien

        reorganiser(self.cfg)
        fichiers = sorted(str(p.relative_to(self.tmp)).replace("\\", "/") for p in self.tmp.rglob("*")
                          if p.is_file() and "_index" not in p.parts and "_rapports" not in p.parts)
        self.assertEqual(fichiers, [
            "Reçus et paiements/Logement/Location/2024/2024-03-05 - Agence - Quittance mars.txt",
            "_Mails sauvegardés/Apple/Boîte de réception/2024/m.eml",
        ])
        self.assertFalse((self.tmp / "Apple").exists())
        self.assertFalse((self.tmp / "Free").exists())
        conn = db.ouvrir(self.cfg.base_index)
        chemins = {r[0] for r in conn.execute("SELECT chemin_local FROM fichiers")}
        eml = conn.execute("SELECT fichier_eml FROM mails_recuperes").fetchone()[0]
        conn.close()
        self.assertEqual(len(chemins), 1)
        self.assertTrue(Path(chemins.pop()).exists())
        self.assertTrue(Path(eml).exists())

        reorganiser(self.cfg)  # deuxième passage : rien ne bouge
        self.assertTrue((self.tmp / "Reçus et paiements" / "Logement" / "Location" / "2024").exists())


class RecuperationComplete(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(dossier_archive=self.tmp, comptes=[Compte("Apple", "icloud", "moi@icloud.com", "h", 993)],
                          recuperation=dict(RECUPERATION_DEFAUT))
        conn = db.ouvrir(self.cfg.base_index)
        with conn:
            conn.execute("INSERT INTO scans(id, date, partiel, termine) VALUES (1, '2026-11-03T17:00:00', 0, 1)")
            conn.execute("INSERT INTO dossiers VALUES (1, 'Apple', 'INBOX', 'INBOX', 'réception', '777', 2)")
            conn.execute("INSERT INTO dossiers VALUES (1, 'Apple', 'Junk', 'Junk', 'spam', '777', 1)")
            for uid in (42, 43):
                conn.execute("INSERT INTO messages (scan_id, compte, dossier, uid, taille, date, adresse, objet, nb_pj) VALUES (1, 'Apple', 'INBOX', ?, 5000, '2024-03-05T10:12:00', "
                             "'agence@immo-dupont.fr', 'Votre quittance de loyer', 1)", (uid,))
            conn.execute("INSERT INTO messages (scan_id, compte, dossier, uid, taille, date, adresse, objet, nb_pj) VALUES (1, 'Apple', 'Junk', 9, 100, '2024-01-01T00:00:00', 'x@y', 'pub', 0)")
            # Numérotation IMAP : 1 = alternative (1.1 texte, 1.2 related : 1.2.1 html, 1.2.2 logo), 2 = PDF, 3 = mail joint
            conn.execute("INSERT INTO pieces (scan_id, compte, dossier, uid, chemin, nom, categorie, taille, statut) VALUES (1, 'Apple', 'INBOX', 42, '2', 'Quittance mars 2024.pdf', 'PDF', 20, 'pièce jointe')")
            conn.execute("INSERT INTO pieces (scan_id, compte, dossier, uid, chemin, nom, categorie, taille, statut) VALUES (1, 'Apple', 'INBOX', 42, '3', 'message_joint.eml', 'Mails joints', 200, 'pièce jointe')")
            conn.execute("INSERT INTO pieces (scan_id, compte, dossier, uid, chemin, nom, categorie, taille, statut) VALUES (1, 'Apple', 'INBOX', 42, '1.2.2', 'logo.png', 'Images / photos', 2000, 'image décorative')")
        conn.close()
        self._orig = (recuperation.Session, credentials.get_password)
        recuperation.Session = FakeSession
        credentials.get_password = lambda c: "x"

    def tearDown(self):
        recuperation.Session, credentials.get_password = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fichiers(self):
        return sorted(str(p.relative_to(self.tmp)).replace("\\", "/") for p in self.tmp.rglob("*")
                      if p.is_file() and "_index" not in p.parts and "_rapports" not in p.parts)

    def test_recuperation_et_reprise(self):
        rapport = recuperation.lancer(self.cfg, ouvrir=False)
        self.assertTrue(rapport.exists())
        fichiers = self._fichiers()
        eml = "_Mails sauvegardés/Apple/Boîte de réception/2024/2024-03-05 10h12 - Agence Dupont - Votre quittance de loyer.eml"
        pdf = "Reçus et paiements/Logement/Location/2024/2024-03-05 - Agence Dupont - Quittance mars 2024.pdf"
        joint = "Reçus et paiements/Logement/Location/2024/2024-03-05 - Agence Dupont - message_joint.eml"
        self.assertEqual(fichiers, sorted([eml, pdf, joint]))  # le logo décoratif n'est pas extrait
        self.assertEqual((self.tmp / pdf).read_bytes(), b"%PDF-1.4 contenu")
        self.assertEqual((self.tmp / eml).read_bytes(), FakeSession.raw)
        self.assertIn(b"mail transf", (self.tmp / joint).read_bytes())

        # Deuxième passage : rien n'est retéléchargé ni dupliqué.
        recuperation.lancer(self.cfg, ouvrir=False)
        self.assertEqual(self._fichiers(), fichiers)

    def test_uidvalidity_change_bloque(self):
        conn = db.ouvrir(self.cfg.base_index)
        with conn:
            conn.execute("UPDATE dossiers SET uidvalidity = '1' WHERE brut = 'INBOX'")
        conn.close()
        recuperation.lancer(self.cfg, ouvrir=False)
        self.assertEqual(self._fichiers(), [])

    def test_sans_scan(self):
        vide = Config(dossier_archive=self.tmp / "vide", comptes=[], recuperation=dict(RECUPERATION_DEFAUT))
        self.assertIsNone(recuperation.lancer(vide, ouvrir=False))


if __name__ == "__main__":
    unittest.main()
