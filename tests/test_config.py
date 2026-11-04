import json
import unittest

from mailarchive import config


class ExampleConfig(unittest.TestCase):
    def test_example_is_valid_and_resolves(self):
        raw = json.loads(config.EXAMPLE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(raw["comptes"], [])  # première utilisation : le menu demande les adresses
        self.assertEqual(raw["recuperation"]["roles"], ["réception", "envoyés", "brouillons"])

    def test_detection_messagerie(self):
        from mailarchive import providers
        self.assertEqual(providers.detecter("x@icloud.com").nom, "Apple")
        self.assertEqual(providers.detecter("x@me.com").hote, "imap.mail.me.com")
        self.assertEqual(providers.detecter("x@free.fr").hote, "imap.free.fr")
        self.assertFalse(providers.detecter("x@hotmail.fr").supporte)
        self.assertIsNone(providers.detecter("x@inconnu.example"))

    def test_dossier_retenu(self):
        r = {"roles": ["réception", "envoyés"], "dossiers_inclus": ["Commercial"], "dossiers_exclus": ["Sent"]}
        self.assertTrue(config.dossier_retenu(r, "réception", "INBOX"))
        self.assertTrue(config.dossier_retenu(r, "autre", "Commercial"))
        self.assertFalse(config.dossier_retenu(r, "envoyés", "Sent"))
        self.assertFalse(config.dossier_retenu(r, "corbeille", "Trash"))


if __name__ == "__main__":
    unittest.main()
