"""Test that an output-only night audit proves XLSX+SQLite without Final writes.

No network, no paid LLM, no production workbooks.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pipeline_produtos as core


class OutputOnlyAuditTests(unittest.TestCase):
    def test_output_only_accepts_missing_cohort_from_intentionally_unchanged_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cohort = root / "cohort.xlsx"
            final = root / "final.xlsx"
            output = root / "classification_output.xlsx"
            cache = root / "isolated.sqlite"
            pd.DataFrame([{"Marca": "ABC", "Modelo": "NEW"}]).to_excel(cohort, index=False)
            pd.DataFrame([{"Marca": "HISTORIC", "Modelo": "OLD", "Revisão B.I": "OK"}]).to_excel(final, index=False)
            with pd.ExcelWriter(output) as writer:
                pd.DataFrame([{"Marca": "ABC", "Modelo": "NEW",
                               "Resolution State": "PENDING_CLASSIFICATION",
                               "Auto Eligible": "NAO", "Review Required": "SIM"}]).to_excel(
                                   writer, sheet_name="Resultados", index=False)
            cache.write_bytes(b"stub")
            (root / "classification_01.log").write_text(
                "NIGHT_CLASSIFICATION_OUTPUT_ONLY_OK\n", encoding="utf-8"
            )
            cfg = {"_nightly_output_only": True, "files": {
                "input": str(cohort), "final": str(final),
                "output": str(output), "cache": str(cache),
            }}
            state = {"stages": {"classification": {"log": "classification_01.log"}}}
            class Cache:
                def __init__(self, path):
                    pass
                def get(self, brand, model):
                    return SimpleNamespace(state="RAW_OK")
                def close(self):
                    pass
            with (patch.object(core, "overnight_examine", return_value=([], {}, 1, 1)),
                  patch.object(core, "TavilyReadOnlyStore", Cache),
                  patch.object(core, "protected_originals", return_value=[])):
                self.assertTrue(core.outcome_audit(root, state, cfg))
                self.assertTrue((root / "quality_audit.json").is_file())
                self.assertEqual(state["quality_audit"]["missing_cohort_in_final"], [])
                self.assertFalse(state["quality_audit"]["canonical_promotion_allowed"])
                self.assertTrue(state["quality_audit"]["classification_output_only"])

    def test_unchanged_final_does_not_pass_normal_baseline_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cohort = root / "cohort.xlsx"
            final = root / "final.xlsx"
            output = root / "classification_output.xlsx"
            cache = root / "isolated.sqlite"
            pd.DataFrame([{"Marca": "ABC", "Modelo": "NEW"}]).to_excel(cohort, index=False)
            pd.DataFrame([{"Marca": "HISTORIC", "Modelo": "OLD", "Revisão B.I": "OK"}]).to_excel(final, index=False)
            with pd.ExcelWriter(output) as writer:
                pd.DataFrame([{"Marca": "ABC", "Modelo": "NEW"}]).to_excel(
                    writer, sheet_name="Resultados", index=False)
            cache.write_bytes(b"stub")
            (root / "classification_01.log").write_text(
                "BASE FINAL INPUT ROW COVERAGE OK\n"
                "BASE FINAL PROCV COVERAGE OK\n"
                "BASE FINAL CLASSIFICATION INTEGRITY\n",
                encoding="utf-8",
            )
            cfg = {"_nightly_output_only": False, "files": {
                "input": str(cohort), "final": str(final),
                "output": str(output), "cache": str(cache),
            }}
            state = {"stages": {"classification": {"log": "classification_01.log"}}}
            class Cache:
                def __init__(self, path):
                    pass
                def get(self, brand, model):
                    return SimpleNamespace(state="RAW_OK")
                def close(self):
                    pass
            with (patch.object(core, "overnight_examine", return_value=([], {}, 1, 1)),
                  patch.object(core, "TavilyReadOnlyStore", Cache),
                  patch.object(core, "protected_originals", return_value=[])):
                self.assertFalse(core.outcome_audit(root, state, cfg))
                self.assertTrue(state["quality_audit"]["missing_cohort_in_final"])


if __name__ == "__main__":
    unittest.main()
