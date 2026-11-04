import unittest

from mailarchive import ia_locale, recuperation, tui
from mailarchive.themes import A_CLASSER, TRI_MANUEL, Classeur, _regles, arborescence
from tests.test_recuperation import RecuperationComplete

REGLES = _regles([{"theme": "Énergie/Électricité - Gaz", "mots": ["edf", "électricité"]}], "theme")
TYPES = _regles([{"type": "Factures", "mots": ["facture"]}], "type")


def classeur(**exclusions):
    return Classeur(REGLES, 2.0, TYPES, exclusions)


class MotsIgnores(unittest.TestCase):
    def test_mot_ignore_ne_compte_plus(self):
        self.assertEqual(classeur().classer("Facture EDF.pdf", "", "", "", "PDF").theme, "Énergie/Électricité - Gaz")
        c = classeur(ignorer=["EDF"])
        self.assertEqual(c.classer("Facture EDF.pdf", "", "", "", "PDF").theme, A_CLASSER)
        self.assertEqual(c.classer_type("Facture EDF.pdf", "", "", "PDF").theme, "Factures")  # le reste compte toujours
        self.assertTrue(c.touche_ignores("Facture EDF.pdf", "", ""))

    def test_accents_majuscules_et_mot_entier(self):
        c = classeur(ignorer=["ELECTRICITE"])
        self.assertEqual(c.classer("x.pdf", "Électricité Électricité", "", "", "PDF").theme, A_CLASSER)
        c = classeur(ignorer=["ed"])
        self.assertEqual(c.classer("Facture EDF.pdf", "", "", "", "PDF").theme, "Énergie/Électricité - Gaz")


class ATrierSoiMeme(unittest.TestCase):
    def test_ni_regles_ni_domaine(self):
        c = classeur(a_trier_soi_meme=["Mamie Jeanne"])
        self.assertEqual(c.classer_type("facture.pdf", "", "Facture EDF pour mamie jeanne", "PDF").theme, TRI_MANUEL)
        self.assertEqual(c.classer("facture.pdf", "", "", "Facture EDF pour mamie jeanne", "PDF").theme, A_CLASSER)
        self.assertEqual(arborescence(TRI_MANUEL, A_CLASSER, "2024", True), [TRI_MANUEL, "2024"])
        self.assertIsNone(c.bloquant("facture.pdf", "", "Facture EDF pour mamie"))


class SaisieEtListes(unittest.TestCase):
    def setUp(self):
        self._save = tui.save_raw
        self.ecrit = []
        tui.save_raw = lambda raw: self.ecrit.append(raw)  # ne jamais toucher au vrai config.json

    def tearDown(self):
        tui.save_raw = self._save

    def test_saisie(self):
        self.assertEqual(tui.saisir_mots(" Projet X ; projet  x, Été,ete ,, "), ["Projet X", "Été"])

    def test_ajout_retrait(self):
        raw = {"confidentiel": {"mots": ["unité a"]}}
        self.assertEqual(tui.ajouter_mots(raw, "confidentiel", ["Unité A", "unité b"]), ["unité b"])
        self.assertEqual(raw["confidentiel"]["mots"], ["unité a", "unité b"])
        self.assertEqual(tui.ajouter_mots(raw, "ignorer", ["signature"]), ["signature"])
        self.assertEqual(raw["exclusions"]["ignorer"], ["signature"])
        self.assertEqual(tui.retirer_mots(raw, "confidentiel", ["UNITE A"]), ["unité a"])
        self.assertEqual(raw["confidentiel"]["mots"], ["unité b"])
        self.assertEqual(len(self.ecrit), 3)


class RecuperationAvecMotATrierSoiMeme(RecuperationComplete):
    def test_range_a_part_et_jamais_propose_a_une_ia(self):
        self.cfg.raw = {"exclusions": {"a_trier_soi_meme": ["quittance"]}}
        recuperation.lancer(self.cfg, ouvrir=False)
        pieces = [f for f in self._fichiers() if not f.startswith("_Mails sauvegardés")]
        self.assertTrue(pieces)
        self.assertTrue(all(f.startswith(f"{TRI_MANUEL}/2024/") for f in pieces), pieces)
        self.assertEqual(ia_locale.candidats(self.cfg), [])


if __name__ == "__main__":
    unittest.main()
