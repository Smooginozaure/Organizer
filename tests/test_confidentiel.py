import email.policy
import json
import shutil
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from mailarchive import credentials, db, recuperation, tri_assiste
from mailarchive.config import Compte, Config, RECUPERATION_DEFAUT
from mailarchive.confidentiel import Filtre

NEUTRE = "Notes sur le jardin et les plantations de printemps, tomates et courgettes. " * 5


class FiltreConfidentiel(unittest.TestCase):
    def setUp(self):
        self.f = Filtre()

    def test_adresses(self):
        for a in ["agent@gendarmerie.interieur.gouv.fr", "x@diplomatie.gouv.fr", "y@interieur.gouv.fr",
                  "z@ambafrance-us.org", "w@consulfrance-montreal.org", "v@intradef.gouv.fr"]:
            self.assertIsNotNone(self.f.adresse(a), a)
        for a in ["moi@icloud.com", "moi@free.fr", "service@edf.fr", ""]:
            self.assertIsNone(self.f.adresse(a), a)

    def test_jargon_et_particuliers(self):
        from mailarchive.confidentiel import echange_entre_particuliers
        for t in ["6 personnels EG", "Emploi au maintien de l'ordre", "GD DUPONT ADC MARTIN", "Visières pare-balles"]:
            self.assertIsNotNone(self.f.texte(t), t)
        # Mots personnels : ajoutés dans config.json (menu « Mots à exclure du tri »), pas dans la liste intégrée.
        self.assertIsNone(self.f.texte("mission à Saint-Exemple"))
        self.assertIsNotNone(Filtre({"mots": ["Saint Exemple"]}).texte("mission à Saint-Exemple"))
        self.assertTrue(echange_entre_particuliers(["a@gmail.com", "moi@icloud.com"]))
        self.assertTrue(echange_entre_particuliers([]))
        self.assertFalse(echange_entre_particuliers(["moi@icloud.com", "factures@edf.fr"]))

    def test_textes(self):
        self.assertIsNotNone(self.f.texte("liste_des_pj_demande_de_passeport_de_service_maj_2025.pdf"))
        self.assertIsNotNone(self.f.texte("Note de l'Escadron de Gendarmerie Mobile"))
        self.assertIsNotNone(self.f.texte("Convocation à l'AMBASSADE de France"))
        self.assertIsNotNone(self.f.texte("Ministère de l'Europe et des Affaires étrangères"))
        self.assertIsNone(self.f.texte("Facture EDF", NEUTRE))

    def test_config_ne_peut_quajouter(self):
        f = Filtre({"mots": ["projet x"], "domaines": ["exemple.fr"]})
        self.assertIsNotNone(f.texte("le projet X"))
        self.assertIsNotNone(f.adresse("a@exemple.fr"))
        self.assertIsNotNone(f.adresse("b@gendarmerie.interieur.gouv.fr"))


def _mail(expediteur, destinataire, objet, nom, contenu: bytes) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = expediteur, destinataire, objet
    m.set_content("Bonjour")
    m.add_attachment(contenu, maintype="application", subtype="octet-stream", filename=nom)
    return m.as_bytes(policy=email.policy.SMTP)


MAILS = {
    # destinataire gendarmerie, document neutre : confidentiel par le mail
    44: ("Moi <moi@icloud.com>", "collegue@gendarmerie.interieur.gouv.fr", "pour info", "notes.txt", (NEUTRE + " (notes)").encode()),
    # adresse perso, mais texte mentionnant une ambassade : confidentiel par le contenu
    45: ("Moi <moi@icloud.com>", "moi@icloud.com", "doc", "doc.txt", ("Courrier de l'ambassade. " + NEUTRE).encode()),
    # PDF scanné, sans texte lisible : jamais exporté
    46: ("Ami <ami@gmail.com>", "moi@icloud.com", "scan", "Scan001.pdf", b"%PDF-1.4 image seulement"),
    # document neutre lisible : le seul exportable
    47: ("Club <contact@club-jardin.fr>", "moi@icloud.com", "jardin", "reunion.txt", NEUTRE.encode()),
    # cas réel : document de travail envoyé à soi-même, sans le mot « gendarmerie » (jargon métier)
    49: ("Moi <moi@free.fr>", "moi@free.fr", "qfr", "etiquettes.txt",
         ("Fiche de séance : 6 personnels EG, gilets pare-balles et boucliers. " + NEUTRE).encode()),
    # document neutre, mais échangé uniquement entre particuliers : exclu par prudence
    50: ("Collègue <collegue@gmail.com>", "moi@icloud.com", "le pdf", "tableau.txt",
         ("Tableau de service de la semaine. " + NEUTRE + " (tableau)").encode()),
    # même fichier que 47, mais dans un mail diplomatique : le fichier devient confidentiel
    48: ("Agent <agent@diplomatie.gouv.fr>", "moi@icloud.com", "re", "reunion.txt", NEUTRE.encode()),
}


class FakeSession:
    def __init__(self, *a, **k):
        self.uidvalidity = ""

    def connect(self):
        pass

    def close(self):
        pass

    def ouvrir(self, d):
        self.uidvalidity = "9"

    def fetch(self, uids, items):
        out = []
        for u in uids:
            raw = _mail(*MAILS[int(u)])
            out += [(f"1 (UID {u} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        return out


class ExportSansConfidentiel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(dossier_archive=self.tmp, comptes=[Compte("Apple", "icloud", "moi@icloud.com", "h", 993)],
                          recuperation=dict(RECUPERATION_DEFAUT))
        conn = db.ouvrir(self.cfg.base_index)
        with conn:
            conn.execute("INSERT INTO scans(id, date, partiel, termine) VALUES (1, '2026-11-03T17:00:00', 0, 1)")
            conn.execute("INSERT INTO dossiers VALUES (1, 'Apple', 'INBOX', 'INBOX', 'réception', '9', 5)")
            for uid, (_e, _d, objet, nom, _c) in MAILS.items():
                conn.execute("INSERT INTO messages (scan_id, compte, dossier, uid, taille, date, adresse, objet, nb_pj) VALUES (1, 'Apple', 'INBOX', ?, 1000, '2025-06-01T09:00:00', '', ?, 1)", (uid, objet))
                cat = "PDF" if nom.endswith(".pdf") else "Documents texte"
                conn.execute("INSERT INTO pieces (scan_id, compte, dossier, uid, chemin, nom, categorie, taille, statut) VALUES (1, 'Apple', 'INBOX', ?, '2', ?, ?, 100, 'pièce jointe')", (uid, nom, cat))
        conn.close()
        self._orig = (recuperation.Session, credentials.get_password)
        recuperation.Session = FakeSession
        credentials.get_password = lambda c: "x"

    def tearDown(self):
        recuperation.Session, credentials.get_password = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_seul_le_document_neutre_est_exportable(self):
        recuperation.lancer(self.cfg, ouvrir=False)
        conn = db.ouvrir(self.cfg.base_index)
        conf = {r["chemin_local"].split("\\")[-1]: r["confidentiel"] for r in conn.execute("SELECT * FROM fichiers")}
        conn.close()
        self.assertEqual(sorted(conf.values()), [0, 0, 1, 1, 1, 1])  # notes, doc, reunion (via 48), etiquettes ; scan et tableau non

        # Le fichier confidentiel non reconnu va dans « À classer - confidentiel », pas dans « À classer ».
        dossiers = {p.name.rsplit(" - ", 1)[-1]: p.parent.parent.name for p in self.tmp.rglob("*.txt")
                    if "_Mails sauvegardés" not in p.parts and "_rapports" not in p.parts}
        self.assertEqual(dossiers, {"notes.txt": "À classer - confidentiel", "doc.txt": "À classer - confidentiel",
                                    "reunion.txt": "À classer - confidentiel", "etiquettes.txt": "À classer - confidentiel",
                                    "tableau.txt": "À classer"})

        export = json.loads(tri_assiste.exporter(self.cfg).read_text(encoding="utf-8"))
        # 47 et 48 partagent le même fichier, devenu confidentiel : rien de lisible et neutre ne reste.
        self.assertEqual(export["elements"], [])
        texte_export = json.dumps(export, ensure_ascii=False).lower()
        for interdit in ("gendarmerie", "ambassade", "diplomatie", "notes.txt", "doc.txt", "reunion", "etiquettes",
                         "personnels", "tableau"):
            self.assertNotIn(interdit, texte_export)

    def test_document_neutre_exporte_puis_reclasse(self):
        del MAILS[48]
        try:
            conn = db.ouvrir(self.cfg.base_index)
            with conn:
                conn.execute("DELETE FROM messages WHERE uid = 48")
                conn.execute("DELETE FROM pieces WHERE uid = 48")
            conn.close()
            recuperation.lancer(self.cfg, ouvrir=False)
            export = json.loads(tri_assiste.exporter(self.cfg).read_text(encoding="utf-8"))
            self.assertEqual([e["fichier"] for e in export["elements"]], ["reunion.txt"])  # ni etiquettes, ni tableau
            ok_id = export["elements"][0]["id"]

            conn = db.ouvrir(self.cfg.base_index)
            conf_id = conn.execute("SELECT rowid FROM fichiers WHERE confidentiel = 1").fetchone()[0]
            conn.close()
            decisions = self.tmp / "decisions.json"
            decisions.write_text(json.dumps({"decisions": [{"id": ok_id, "type": "Notices, guides et présentations", "domaine": "Loisirs/Randonnée"},
                                                           {"id": conf_id, "type": "Courriers", "domaine": "Divers"}]}), encoding="utf-8")
            self.assertEqual(tri_assiste.appliquer(self.cfg, decisions), 1)  # 1 refus : le confidentiel
            self.assertTrue(list((self.tmp / "Notices, guides et présentations" / "Loisirs" / "Randonnée" / "2025").glob("*reunion.txt")))
            self.assertEqual(len(list((self.tmp / "À classer - confidentiel").rglob("*.txt"))), 3)

            journal = next((self.tmp / "_tri_assiste").glob("journal_*.csv"))
            tri_assiste.annuler(self.cfg, journal)
            self.assertTrue(list((self.tmp / "À classer" / "2025").glob("*reunion.txt")))
        finally:
            MAILS[48] = ("Agent <agent@diplomatie.gouv.fr>", "moi@icloud.com", "re", "reunion.txt", NEUTRE.encode())


if __name__ == "__main__":
    unittest.main()
