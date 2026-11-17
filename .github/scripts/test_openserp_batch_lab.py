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
        self.assertEqual(len(lab.parse_queries("STIHL MS382\\nSTIHL FS250\\nNOVABOT N2000")), 3)

    def test_placeholder_and_duplicate_blocked(self):
        for value in ("MARCA_REAL MODELO_REAL", "STIHL MS382\\nSTIHL MS-382",
                      "STIHL MS382\\nSTIHL MS382"):
            with self.assertRaises(ValueError):
                lab.parse_queries(value)

    def test_model_normalization(self):
        self.assertTrue(lab.model_present("STIHL MS-382 chainsaw", "MS382"))
        self.assertTrue(lab.model_present("STIHL FS 250", "FS250"))
        self.assertFalse(lab.model_present("STIHL FS 251", "FS250"))

    def test_google_redirect_not_promoted(self):
        row = lab.classify_candidate("STIHL MS382", {
            "title": "STIHL MS-382 Chainsaw", "snippet": "Petrol chain saw",
            "url": "https://www.google.com/goto?url=opaque", "rank": 1})
        self.assertEqual(row["reason"], "UNRESOLVED_OR_UNSAFE_URL")
        self.assertIsNone(row["direct_url"])
        self.assertFalse(row["evidence_approved"])

    def test_accessory_not_equipment(self):
        row = lab.classify_candidate("NOVABOT N2000", {
            "title": "NOVABOT N2000 protective cover",
            "url": "https://example.com/cover", "rank": 3})
        self.assertEqual(row["reason"], "ACCESSORY_OR_PART")
        self.assertFalse(row["classification_approved"])

    def test_direct_identity_is_still_unapproved(self):
        row = lab.classify_candidate("STIHL FS250", {
            "title": "STIHL FS 250 Brushcutter",
            "url": "https://stihl.example.org/fs250", "rank": 1})
        self.assertEqual(row["reason"], "IDENTITY_CANDIDATE")
        self.assertFalse(row["evidence_approved"])

    def test_partial_results_not_approved(self):
        result = lab.process_response("STIHL MS382", "google", {
            "results": [{"type": "organic", "rank": 1,
                         "title": "STIHL MS 382 Chainsaw",
                         "url": "https://example.com/ms382"}]})
        self.assertEqual(result["identity_candidates"], 1)
        self.assertFalse(result["classification_approved"])

    def test_engine_failure_is_not_success(self):
        result = lab.process_response("STIHL MS382", "google", {
            "meta": {"engines_failed": ["google"]},
            "results": [{"type": "organic", "rank": 1,
                         "title": "STIHL MS382 Chainsaw",
                         "url": "https://example.com/ms382"}]})
        self.assertEqual(result["status"], "ENGINE_FAILED")


if __name__ == "__main__":
    unittest.main()
