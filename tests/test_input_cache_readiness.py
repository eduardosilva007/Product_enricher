"""Offline budget/readiness counts for 900-SKU preflight without API calls."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pipeline_produtos as p


class ReadinessNoApiTests(unittest.TestCase):
    def test_raw_empty_not_fresh_collection_and_accepted_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pd.DataFrame([
                {"Marca": "A", "Modelo": "R1"},
                {"Marca": "A", "Modelo": "R1"},
                {"Marca": "A", "Modelo": "E1"},
                {"Marca": "A", "Modelo": "M1"},
                {"Marca": "A", "Modelo": "L1"},
            ]).to_excel(root / "input.xlsx", index=False)
            (root / "cache.sqlite").touch()
            cfg = {"files": {
                "input": str(root / "input.xlsx"),
                "cache": str(root / "cache.sqlite"),
                "master": str(root / "master.xlsx"),
                "final": str(root / "final.xlsx"),
            }}
            class FakeStore:
                def __init__(self, path):
                    pass
                def get(self, brand, model):
                    return SimpleNamespace(state={"R1": "RAW_OK", "E1": "RAW_EMPTY",
                                                  "M1": "RAW_MISSING", "L1": "RAW_OK"}[model])
                def close(self):
                    pass
            with (patch.object(p, "ROOT", root),
                  patch.object(p, "TavilyReadOnlyStore", FakeStore),
                  patch.object(p, "load_baseline_inventory",
                               return_value={"processed": {"a|l1"}})):
                result = p.input_cache_readiness(cfg)
            c = result["counts"]
            self.assertEqual(c["input_rows_valid"], 5)
            self.assertEqual(c["input_unique_skus"], 4)
            self.assertEqual(c["input_duplicate_skus"], 1)
            self.assertEqual(c["RAW_EMPTY"], 1)
            self.assertEqual(c["raw_empty_requires_explicit_recollection_approval"], 1)
            self.assertEqual(c["raw_missing_fresh_collection_candidates"], 1)
            self.assertEqual(c["unaccepted_with_raw_ok"], 1)
            self.assertEqual(c["already_accepted_or_human"], 1)
            self.assertEqual(result["search_calls"], 0)
            self.assertEqual(result["llm_calls"], 0)


if __name__ == "__main__":
    unittest.main()
