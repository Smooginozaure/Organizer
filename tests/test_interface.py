import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from mailarchive import config as cfgmod
from mailarchive import credentials, db, emplacement, recherche, recuperation, veille
from tests.test_recuperation import RecuperationComplete

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class ModesDeRecuperation(RecuperationComplete):
    """Pièces jointes seules d'abord, puis mail complet : rien n'est perdu ni dupliqué."""

    def _fichiers_hors_mails(self):
        return [f for f in self._fichiers() if not f.startswith("_Mails")]

    def test_pj_puis_complet(self):
        est = recuperation.reste_a_faire(self.cfg, sauvegarder=False)
        self.assertEqual((est["pj"], est["mails"]), (2, 0))
        recuperation.lancer(self.cfg, ouvrir=False, sauvegarder=False)
        self.assertEqual([f for f in self._fichiers() if f.startswith("_Mails")], [])
        pj = self._fichiers_hors_mails()
        self.assertEqual(len(pj), 2)

        est = recuperation.reste_a_faire(self.cfg, sauvegarder=True)
        self.assertEqual((est["pj"], est["mails"]), (0, 2))  # 2 mails inventoriés, dont 1 disparu du serveur
        recuperation.lancer(self.cfg, ouvrir=False, sauvegarder=True)
        self.assertEqual(self._fichiers_hors_mails(), pj)  # pièces jointes non dupliquées
        self.assertEqual(len([f for f in self._fichiers() if f.startswith("_Mails")]), 1)
        self.assertEqual(recuperation.DERNIER_BILAN.doublons, 0)

    def test_periode(self):
        self.assertEqual(recuperation.reste_a_faire(self.cfg, True, depuis="2025-01-01")["mails"], 0)
        recuperation.lancer(self.cfg, ouvrir=False, sauvegarder=True, depuis="2025-01-01")
        self.assertEqual(self._fichiers(), [])


class Recherche(RecuperationComplete):
    def test_recherche(self):
        recuperation.lancer(self.cfg, ouvrir=False)
        res = recherche.rechercher(self.cfg, "quittance 2024")
        self.assertTrue(res)
        self.assertTrue(all(Path(r["chemin_local"]).exists() for r in res))
        self.assertEqual(recherche.rechercher(self.cfg, "facture edf"), [])
        self.assertTrue(recherche.rechercher(self.cfg, "QUITT"))  # début de mot, sans majuscules


class Emplacement(RecuperationComplete):
    def setUp(self):
        super().setUp()
        self._raw = (cfgmod.load_raw, cfgmod.save_raw, emplacement.load_raw, emplacement.save_raw)
        self.sauve = {}
        emplacement.load_raw = lambda: {"dossier_archive": str(self.cfg.dossier_archive)}
        emplacement.save_raw = lambda raw: self.sauve.update(raw)

    def tearDown(self):
        emplacement.load_raw, emplacement.save_raw = self._raw[2], self._raw[3]
        super().tearDown()

    def test_deplacement_meme_disque(self):
        recuperation.lancer(self.cfg, ouvrir=False)
        avant = self._fichiers()
        nouveau = self.tmp.parent / (self.tmp.name + "_deplace")
        try:
            emplacement.deplacer_archive(self.tmp, nouveau)
            self.assertFalse(self.tmp.exists())
            self.assertEqual(self.sauve["dossier_archive"], str(nouveau.resolve()))
            conn = db.ouvrir(nouveau / "_index" / "archive.sqlite")
            chemins = [r[0] for r in conn.execute("SELECT chemin_local FROM fichiers")] + \
                      [r[0] for r in conn.execute("SELECT fichier_eml FROM mails_recuperes WHERE fichier_eml IS NOT NULL")]
            conn.close()
            self.assertTrue(chemins and all(Path(c).exists() and str(nouveau.resolve()) in c for c in chemins))
            self.assertEqual(sorted(str(p.relative_to(nouveau)).replace("\\", "/") for p in nouveau.rglob("*")
                                    if p.is_file() and "_index" not in p.parts and "_rapports" not in p.parts), avant)
            with self.assertRaises(ValueError):
                emplacement.deplacer_archive(nouveau, nouveau / "sous-dossier")
        finally:
            shutil.copytree(nouveau, self.tmp, dirs_exist_ok=True)
            shutil.rmtree(nouveau, ignore_errors=True)


class Planification(unittest.TestCase):
    def test_xml_valide(self):
        for v, attendu in [
            ({"frequence": "quotidienne", "heure": "08:30", "intervalle_h": 6, "jour": "lundi", "mode": "complet"}, "ScheduleByDay"),
            ({"frequence": "horaire", "heure": "08:00", "intervalle_h": 6, "jour": "lundi", "mode": "pj"}, "Repetition"),
            ({"frequence": "hebdomadaire", "heure": "20:00", "intervalle_h": 6, "jour": "dimanche", "mode": "pj"}, "ScheduleByWeek"),
        ]:
            racine = ET.fromstring(veille._xml(v).split("?>", 1)[1])
            self.assertIsNotNone(racine.find(f".//t:{attendu}", NS), attendu)
            self.assertEqual(racine.find(".//t:DisallowStartIfOnBatteries", NS).text, "false")
            self.assertEqual(racine.find(".//t:StartWhenAvailable", NS).text, "true")
            self.assertTrue(racine.find(".//t:Arguments", NS).text.endswith('veille.pyw"'))
        self.assertIn("Sunday", veille._xml({"frequence": "hebdomadaire", "heure": "20:00", "intervalle_h": 1,
                                             "jour": "dimanche", "mode": "pj"}))

    def test_verrou(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = cfgmod.Config(dossier_archive=tmp)
            with veille.verrou(cfg):
                with self.assertRaises(veille.Occupe):
                    with veille.verrou(cfg):
                        pass
            with veille.verrou(cfg):  # libéré après usage
                pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
