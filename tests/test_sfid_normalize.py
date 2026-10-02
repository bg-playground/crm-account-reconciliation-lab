import random
import unittest

from recon_lab.normalize import Normalizer, Soundex
from recon_lab.sfid import SalesforceId


class SalesforceIdTests(unittest.TestCase):
    def test_known_suffix(self):
        self.assertEqual(SalesforceId.to18("000000000000Abc"), "000000000000AbcAAE")

    def test_all_lower_and_all_upper_blocks(self):
        self.assertEqual(SalesforceId.suffix("aaaaabbbbbccccc"), "AAA")
        self.assertEqual(SalesforceId.suffix("AAAAABBBBBCCCCC"), "555")

    def test_make_is_valid_and_prefixed(self):
        rng = random.Random(1)
        for _ in range(50):
            value = SalesforceId.make("001", "5A", rng)
            self.assertTrue(SalesforceId.is_valid18(value))
            self.assertTrue(value.startswith("0015A"))

    def test_invalid(self):
        self.assertFalse(SalesforceId.is_valid18("000000000000AbcAAA"))
        self.assertFalse(SalesforceId.is_valid18("short"))


class SoundexTests(unittest.TestCase):
    def test_reference_codes(self):
        for word, code in [("Robert", "R163"), ("Rupert", "R163"), ("Tymczak", "T522"),
                           ("Pfister", "P236"), ("Ashcraft", "A261")]:
            self.assertEqual(Soundex.encode(word), code, word)

    def test_empty(self):
        self.assertIsNone(Soundex.encode(""))
        self.assertIsNone(Soundex.encode(None))


class NormalizerTests(unittest.TestCase):
    def test_name(self):
        self.assertEqual(Normalizer.name("Quorvane Systems, Inc."), "quorvane systems")
        self.assertEqual(Normalizer.name("QUORVANE & Sons LLC"), "quorvane and sons")
        self.assertIsNone(Normalizer.name("Inc."))

    def test_domain(self):
        for raw in ["https://www.quorvane.example/about", "WWW.QUORVANE.EXAMPLE", "http://quorvane.example",
                    "quorvane.example", "https://us.quorvane.example/"]:
            self.assertEqual(Normalizer.domain(raw), "quorvane.example", raw)
        self.assertIsNone(Normalizer.domain(""))
        self.assertIsNone(Normalizer.domain("localhost"))

    def test_phone(self):
        for raw in ["(415) 555-0123", "415-555-0123", "+1 415 555 0123", "1-415-555-0123", "415.555.0123"]:
            self.assertEqual(Normalizer.phone(raw), "4155550123", raw)
        self.assertIsNone(Normalizer.phone("555-0123"))

    def test_postcode_street_state(self):
        self.assertEqual(Normalizer.postcode5("94107-1234"), "94107")
        self.assertIsNone(Normalizer.postcode5("941"))
        self.assertEqual(Normalizer.street("12 Main Street Suite 400"), "12 main st ste 400")
        self.assertEqual(Normalizer.street("12 Main St Ste 400"), "12 main st ste 400")
        self.assertEqual(Normalizer.state(" ca "), "CA")

    def test_record_keys(self):
        rec = Normalizer.record({"Id": "001X", "Name": "Brava Labs LLC", "Website": "brava.example",
                                 "Phone": "", "BillingPostalCode": "02110", "BillingState": "MA"}, "org_a")
        self.assertEqual(rec["unique_id"], "org_a:001X")
        self.assertEqual(rec["name4"], "brav")
        self.assertIsNone(rec["phone10"])
        self.assertEqual(rec["soundex_first"], "B610")


if __name__ == "__main__":
    unittest.main()
