import unittest

from mailarchive.classify import DECO, PJ, QUARANTINE, analyze_bodystructure
from mailarchive.imap_client import Dossier
from mailarchive.imap_parse import (
    decode_imap_utf7,
    envelope_sender,
    envelope_subject,
    parse_fetch,
    parse_list,
)
from mailarchive.scan import date_interne

# Mail typique : texte + HTML avec logo intégré + PDF joint (nom encodé RFC 2231)
# + un sujet transmis en littéral IMAP, comme le fait imaplib.
FETCH_DATA = [
    (
        b'1 (UID 42 RFC822.SIZE 250000 INTERNALDATE "05-Mar-2024 10:12:00 +0100" ENVELOPE '
        b'("Tue, 05 Mar 2024 10:12:00 +0100" {27}',
        b'Votre "facture" =?utf-8?Q?=C3=A9?=',
    ),
    b' (("EDF" NIL "service.client" "edf.fr")) NIL NIL ((NIL NIL "moi" "icloud.com")) NIL NIL NIL "<id@edf>") '
    b'BODYSTRUCTURE ((("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 1200 30 NIL NIL NIL NIL)'
    b'(("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 5000 100 NIL NIL NIL NIL)'
    b'("IMAGE" "PNG" ("NAME" "logo.png") "<logo@edf>" NIL "BASE64" 40000 NIL ("INLINE" ("FILENAME" "logo.png")) NIL NIL)'
    b' "RELATED" ("BOUNDARY" "b2") NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "b1") NIL NIL NIL)'
    b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 200000 NIL ("ATTACHMENT" ("FILENAME*" "utf-8\'\'Facture%20%C3%A9lec.pdf")) NIL NIL)'
    b' "MIXED" ("BOUNDARY" "b0") NIL NIL NIL))',
    b'2 (UID 43 RFC822.SIZE 900 INTERNALDATE "06-Mar-2024 08:00:00 +0000" ENVELOPE (NIL "Bonjour" NIL NIL NIL NIL NIL NIL NIL NIL) '
    b'BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "us-ascii") NIL NIL "7BIT" 500 10 NIL NIL NIL NIL))',
]


class FetchParsing(unittest.TestCase):
    def setUp(self):
        self.msgs = parse_fetch(FETCH_DATA)

    def test_two_messages(self):
        self.assertEqual([m["UID"] for m in self.msgs], ["42", "43"])

    def test_envelope_with_literal(self):
        env = self.msgs[0]["ENVELOPE"]
        self.assertEqual(envelope_subject(env), 'Votre "facture" é')
        self.assertEqual(envelope_sender(env), ("EDF", "service.client@edf.fr"))

    def test_attachments(self):
        pieces = analyze_bodystructure(self.msgs[0]["BODYSTRUCTURE"])
        self.assertEqual([(p.nom, p.statut) for p in pieces], [("logo.png", DECO), ("Facture élec.pdf", PJ)])
        pdf = pieces[1]
        self.assertEqual(pdf.categorie, "PDF")
        self.assertEqual(pdf.chemin, "2")
        self.assertEqual(pdf.taille, int(200000 * 57 / 78))

    def test_plain_message_has_no_attachment(self):
        self.assertEqual(analyze_bodystructure(self.msgs[1]["BODYSTRUCTURE"]), [])

    def test_date(self):
        self.assertEqual(date_interne(self.msgs[0]["INTERNALDATE"]).year, 2024)


class Classification(unittest.TestCase):
    def _single(self, part):
        return analyze_bodystructure(part)

    def test_apple_mail_inline_photo_counts(self):
        photo = ["IMAGE", "JPEG", ["NAME", "IMG_0001.jpeg"], "<x@y>", None, "BASE64", 3_000_000, None,
                 ["INLINE", ["FILENAME", "IMG_0001.jpeg"]], None, None]
        [p] = self._single([["TEXT", "PLAIN", None, None, None, "7BIT", 10, 1], photo, "MIXED"])
        self.assertEqual((p.statut, p.categorie), (PJ, "Images / photos"))

    def test_smime_signature_ignored(self):
        sig = ["APPLICATION", "PKCS7-SIGNATURE", ["NAME", "smime.p7s"], None, None, "BASE64", 5000, None,
               ["ATTACHMENT", ["FILENAME", "smime.p7s"]], None, None]
        self.assertEqual(self._single([["TEXT", "PLAIN", None, None, None, "7BIT", 10, 1], sig, "SIGNED"]), [])

    def test_forwarded_mail(self):
        fwd = ["MESSAGE", "RFC822", None, None, None, "7BIT", 8000,
               [None] * 10, ["TEXT", "PLAIN", None, None, None, "7BIT", 10, 1], 50, None, None, None]
        [p] = self._single([["TEXT", "PLAIN", None, None, None, "7BIT", 10, 1], fwd, "MIXED"])
        self.assertEqual((p.categorie, p.nom), ("Mails joints", "message_joint.eml"))

    def test_executable_quarantined(self):
        exe = ["APPLICATION", "OCTET-STREAM", ["NAME", "setup.exe"], None, None, "BASE64", 100000, None,
               ["ATTACHMENT", ["FILENAME", "setup.exe"]], None, None]
        [p] = self._single(exe)
        self.assertEqual(p.categorie, QUARANTINE)

    def test_encoded_word_filename(self):
        doc = ["APPLICATION", "PDF", ["NAME", "=?UTF-8?B?Q29udHJhdCBkZSBsb2NhdGlvbi5wZGY=?="], None, None,
               "BASE64", 1000, None, None, None, None]
        [p] = self._single(doc)
        self.assertEqual(p.nom, "Contrat de location.pdf")


class Folders(unittest.TestCase):
    def test_list_and_roles(self):
        data = [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"',
            b'(\\HasNoChildren) "/" "&AMk-l&AOk-ments envoy&AOk-s"',
            (b'(\\HasNoChildren) "/" {11}', b'Mes "notes"'),
            b'(\\Noselect \\HasChildren) "/" "Archives"',
        ]
        dossiers = [Dossier(brut=n, nom=decode_imap_utf7(n), drapeaux=f) for f, _d, n in parse_list(data)]
        self.assertEqual([d.nom for d in dossiers],
                         ["INBOX", "Deleted Messages", "Éléments envoyés", 'Mes "notes"', "Archives"])
        self.assertEqual([d.role for d in dossiers], ["réception", "corbeille", "envoyés", "autre", "autre"])
        self.assertFalse(dossiers[-1].selectionnable)


if __name__ == "__main__":
    unittest.main()
