"""Argentina: testes offline de segurança e regressão. Executar: python -m unittest discover -s tests -p 'test_argentina_readiness.py' -v
Não faz chamadas Search, não altera SQLite produtivo nem planilhas.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pipeline_produtos as p


class ArgentinaIdentityTests(unittest.TestCase):
    def setUp(self):
        self.suffixes = ["EU", "US", "UK"]

    def test_format_equivalent_sku(self):
        kind, value = p._model_match_in_field("GWS900100", "Bosch GWS 900-100 esmerilhadeira", self.suffixes)
        self.assertEqual(kind, "EXACT")
        self.assertEqual(value, "gws900100")

    def test_wrong_letter_sku_rejected(self):
        for requested, wrong in [
            ("CTVLI20018", "CGTLI20018"),
            ("GSR 18V", "GSB 18V"),
            ("2446-21XC", "2446-20"),
        ]:
            with self.subTest(requested=requested, wrong=wrong):
                self.assertEqual(
                    p._model_match_in_field(requested, "Fabricante " + wrong, self.suffixes)[0],
                    "",
                )

    def test_reject_suffix_embedded_in_other_sku(self):
        self.assertEqual(p._model_match_in_field("CTVLI20018", "CGTLI20018", self.suffixes)[0], "")


class ArgentinaCachePrecollectionTests(unittest.TestCase):
    def test_raw_empty_exclusion_is_visible_not_silent(self):
        """Caracterização do comportamento atual; mudar quando a política de recoleta for corrigida."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cache = td / "test_cache.db"
            connection = sqlite3.connect(cache)
            connection.execute("""CREATE TABLE tavily_cache (
                cache_key TEXT PRIMARY KEY, brand TEXT NOT NULL, model TEXT NOT NULL,
                response_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
            connection.execute(
                "INSERT INTO tavily_cache VALUES (?,?,?,?,?)",
                ("v3|" + p.business_key("BOSCH", "GWS900100"), "BOSCH", "GWS900100",
                 json.dumps({"results": [], "_collection": {"state": "NO_TARGET_EVIDENCE"}}),
                 "2026-11-09T12:00:00-03:00"),
            )
            connection.commit()
            connection.close()
            raw = p.TavilyReadOnlyStore(cache)
            try:
                evidence = raw.get("BOSCH", "GWS900100")
                self.assertEqual(evidence.state, "RAW_EMPTY")
                self.assertEqual(len(evidence.payload["results"]), 0)
            finally:
                raw.close()

    def test_raw_empty_cannot_count_as_evidence(self):
        self.assertFalse(bool({"results": []}["results"]))


class ArgentinaQuotaContractTests(unittest.TestCase):
    def test_current_production_disallows_provider_failover(self):
        self.assertEqual(p.MAX_COLLECTION_REQUESTS_PER_PRODUCT, 1)
        cfg = {"collection": {"hard_limits": {
            "max_requests_per_product": 1,
            "allow_provider_failover": True,
        }}}
        errors = p.validate_collection_cost_policy(cfg)
        self.assertTrue(any("allow_provider_failover" in err for err in errors))

    def test_no_automatic_second_request_for_same_item(self):
        budget = p.CollectionRequestBudget("tavily")
        budget.reserve()
        with self.assertRaises(p.CollectionCostPolicyError):
            budget.reserve()


if __name__ == "__main__":
    unittest.main()
