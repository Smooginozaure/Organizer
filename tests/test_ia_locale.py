import json
import unittest

from mailarchive import db, ia_locale, tri_assiste
from mailarchive.themes import A_CLASSER, AUTRES
from tests.test_recuperation import RecuperationComplete


class AdresseLocaleUniquement(unittest.TestCase):
    def test_refus_hors_ordinateur(self):
        for url in ("https://api.exemple.com/api/generate", "http://192.168.1.20:11434/api/tags", "http://ollama.exemple.fr"):
            with self.subTest(url=url), self.assertRaises(PermissionError):
                ia_locale._verifier_local(url)
        for url in ("http://127.0.0.1:11434/api/tags", "http://localhost:11434/api/tags"):
            ia_locale._verifier_local(url)


class VerificationDesReponses(unittest.TestCase):
    def setUp(self):
        self._appel = ia_locale._appel
        self.modele = ia_locale.Modele("faux", 10, False, "1B")

    def tearDown(self):
        ia_locale._appel = self._appel

    def _repondre(self, texte):
        ia_locale._appel = lambda chemin, data=None, timeout=600: {"response": texte}
        return ia_locale.classer(self.modele, "x.pdf", "", "", "texte", None, ["Factures", AUTRES], ["Santé", A_CLASSER])

    def test_valeurs_hors_liste_ne_deplacent_rien(self):
        self.assertEqual(self._repondre('{"type": "Facture inventée", "domaine": "Mars", "confiance": 5}'),
                         {"type": AUTRES, "domaine": A_CLASSER, "confiance": 1.0})
        self.assertEqual(self._repondre("pas du JSON")["type"], AUTRES)
        self.assertEqual(self._repondre('{"type": "Factures", "domaine": "Santé", "confiance": "?"}')["confiance"], 0.0)

    def test_seuil_et_propositions_inutiles(self):
        props = [{"type": "Factures", "domaine": "Santé", "confiance": 0.9},
                 {"type": "Factures", "domaine": "Santé", "confiance": 0.3},
                 {"type": AUTRES, "domaine": A_CLASSER, "confiance": 1.0},
                 {"type": "Factures", "domaine": "Santé", "confiance": 0.9, "erreur": "délai"}]
        self.assertEqual(len(ia_locale.utiles(props, 0.6)), 1)


class ConfidentielsSeulementEnLocal(RecuperationComplete):
    def test_appliquer(self):
        from mailarchive import recuperation

        recuperation.lancer(self.cfg, ouvrir=False)
        conn = db.ouvrir(self.cfg.base_index)
        fid = conn.execute("SELECT rowid FROM fichiers LIMIT 1").fetchone()[0]
        conn.execute("UPDATE fichiers SET confidentiel = 1 WHERE rowid = ?", (fid,))
        conn.commit()
        conn.close()
        decisions = self.tmp / "d.json"
        decisions.write_text(json.dumps({"decisions": [{"id": fid, "type": "Factures", "domaine": "Santé"}]}), encoding="utf-8")
        self.assertEqual(tri_assiste.appliquer(self.cfg, decisions), 1)  # agent en ligne : refusé
        self.assertEqual(tri_assiste.appliquer(self.cfg, decisions, autoriser_confidentiel=True), 0)  # IA locale : accepté
        conn = db.ouvrir(self.cfg.base_index)
        self.assertEqual(conn.execute("SELECT type_doc FROM fichiers WHERE rowid = ?", (fid,)).fetchone()[0], "Factures")
        conn.close()


if __name__ == "__main__":
    unittest.main()
