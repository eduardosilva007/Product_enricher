"""Offline safety regression tests; never call external providers."""
import importlib.util
import unittest
from pathlib import Path

source = Path(__file__).with_name("openserp_batch_lab.py")
spec = importlib.util.spec_from_file_location("openserp_lab", source)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class ContractTests(unittest.TestCase):
    def test_three_real_queries(self):
        rows = lab.parse_skus("STIHL|MS382\nSTIHL|FS250\nNOVABOT|N2000")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["query"], "STIHL MS382")

    def test_multiword_brand_is_preserved(self):
        row = lab.parse_skus("BRIGGS & STRATTON|40U7770010")[0]
        self.assertEqual(row["brand"], "BRIGGS & STRATTON")
        self.assertEqual(row["model"], "40U7770010")
        self.assertEqual(row["query"], "BRIGGS & STRATTON 40U7770010")

    def test_ambiguous_whitespace_contract_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "EXPECTED_EXPLICIT_BRAND_PIPE_MODEL"):
            lab.parse_skus("BRIGGS & STRATTON 40U7770010")

    def test_placeholder_and_duplicate_blocked(self):
        for value in ("MARCA_REAL|MODELO_REAL", "STIHL|MS382\nSTIHL|MS-382",
                      "STIHL|MS382\nSTIHL|MS382"):
            with self.assertRaises(ValueError):
                lab.parse_skus(value)

    def test_model_normalization(self):
        self.assertTrue(lab.model_present("STIHL MS-382 chainsaw", "MS382"))
        self.assertTrue(lab.model_present("STIHL FS 250", "FS250"))
        self.assertFalse(lab.model_present("STIHL FS 251", "FS250"))

    def test_google_redirect_not_promoted(self):
        row = lab.classify_candidate("STIHL", "MS382", "STIHL MS382", {
            "title": "STIHL MS-382 Chainsaw", "snippet": "Petrol chain saw",
            "url": "https://www.google.com/goto?url=opaque", "rank": 1})
        self.assertEqual(row["reason"], "UNRESOLVED_OR_UNSAFE_URL")
        self.assertIsNone(row["direct_url"])
        self.assertFalse(row["evidence_approved"])

    def test_accessory_not_equipment(self):
        row = lab.classify_candidate("NOVABOT", "N2000", "NOVABOT N2000", {
            "title": "NOVABOT N2000 protective cover",
            "url": "https://example.com/cover", "rank": 3})
        self.assertEqual(row["reason"], "ACCESSORY_OR_PART")
        self.assertFalse(row["classification_approved"])

    def test_direct_identity_is_still_unapproved(self):
        row = lab.classify_candidate("STIHL", "FS250", "STIHL FS250", {
            "title": "STIHL FS 250 Brushcutter",
            "url": "https://stihl.example.org/fs250", "rank": 1})
        self.assertEqual(row["reason"], "IDENTITY_CANDIDATE")
        self.assertFalse(row["evidence_approved"])

    def test_import_shape_contains_organic(self):
        result = lab.process_response("STIHL", "MS382", "STIHL MS382", "google", {
            "results": [{"type": "organic", "rank": 1,
                         "title": "STIHL MS 382 Chainsaw",
                         "url": "https://example.com/ms382"}]})
        self.assertEqual(result["identity_candidates"], 1)
        self.assertEqual(result["organic"][0]["title"], "STIHL MS 382 Chainsaw")
        self.assertFalse(result["classification_approved"])

    def test_engine_failure_is_not_success(self):
        result = lab.process_response("STIHL", "MS382", "STIHL MS382", "google", {
            "meta": {"engines_failed": ["google"]},
            "results": [{"type": "organic", "rank": 1,
                         "title": "STIHL MS382 Chainsaw",
                         "url": "https://example.com/ms382"}]})
        self.assertEqual(result["status"], "ENGINE_FAILED")


if __name__ == "__main__":
    unittest.main()
