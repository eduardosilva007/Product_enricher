"""Bulk-run filter: current complete review proposals should not starve new SKUs."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from pipeline_produtos import (
    PIPELINE_VERSION, Rules, _current_proposal_keys_for_queue,
    build_classification_cohort, business_key,
)


class FakeCatalog:
    def catalog(self):
        return [("BRAND", f"SKU-{i:04d}", business_key("BRAND", f"SKU-{i:04d}"))
                for i in range(400)]

    def get(self, brand, model):
        return SimpleNamespace(state="RAW_OK", payload={
            "_collection": {"state": "READY_FOR_CLASSIFICATION",
                            "curated_results": [{"title": "Some verified title"}]},
        })


class CurrentProposalQueueTests(unittest.TestCase):
    def setUp(self):
        self.rules = Rules(Path(__file__).resolve().parents[1] / "regras_negocio.json")
        self.base = {
            "Marca": "BRAND", "Modelo": "SKU-0000",
            "O que é (Tipo)": "DRILL",
            "Category General": "OTHERS", "FAMILY": "DRILLERS",
            "Título Real": "BRAND cordless drill",
            "Status": "Script", "Revisão B.I": "", "Revisão": "PROVÁVEL",
            "Resolution State": "CLASSIFIED_CLEF",
            "Review Required": "SIM",
            "Rules Hash": self.rules.hash,
            "Pipeline Version": PIPELINE_VERSION,
        }

    def test_only_current_complete_proposals_are_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "master.xlsx"
            records = [
                self.base,
                {**self.base, "Modelo": "SKU-0001", "Rules Hash": "stale"},
                {**self.base, "Modelo": "SKU-0002", "FAMILY": ""},
                {**self.base, "Modelo": "SKU-0003", "Resolution State": "REVIEW"},
                {**self.base, "Modelo": "SKU-0004", "Pipeline Version": "old"},
                {**self.base, "Modelo": "SKU-0005", "Review Required": "NÃO"},
                {**self.base, "Modelo": "SKU-0006", "Revisão": "REVISAR"},
            ]
            pd.DataFrame(records).to_excel(path, index=False)
            skip = _current_proposal_keys_for_queue(path, self.rules)
            self.assertEqual(skip, {business_key("BRAND", "SKU-0000")})

    def test_queue_limit_applies_after_provisional_skip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "master.xlsx"
            rows = [{**self.base, "Modelo": f"SKU-{i:04d}"} for i in range(200)]
            pd.DataFrame(rows).to_excel(path, index=False)
            skip = _current_proposal_keys_for_queue(path, self.rules)
            self.assertEqual(len(skip), 200)
            _, targets, stats, _ = build_classification_cohort(
                FakeCatalog(), processed=skip, manual=set(), max_items=100)
            self.assertEqual(len(targets), 100)
            self.assertEqual(targets[0][1], "SKU-0200")
            self.assertEqual(stats["cohort_already_classified"], 200)


if __name__ == "__main__":
    unittest.main()
