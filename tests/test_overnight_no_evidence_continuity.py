"""No-network tests: a missing evidence SKU stays pending without stopping a campaign."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pipeline_produtos as pp


class OvernightNoEvidenceContinuityTests(unittest.TestCase):
    def test_nine_usable_one_empty_still_passes_batch_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pairs = [("BRAND", f"MODEL-{n}") for n in range(10)]
            input_file = root / "input.xlsx"
            final_file = root / "final.xlsx"
            output_file = root / "output.xlsx"
            cache_file = root / "cache.sqlite"
            pd.DataFrame(pairs, columns=["Marca", "Modelo"]).to_excel(
                input_file, index=False)
            pd.DataFrame(pairs, columns=["Marca", "Modelo"]).to_excel(
                final_file, index=False)
            # A classifier hallucination on the empty-RAW item must be ignored
            # by the cohort auditor and never promoted as an accepted label.
            records = []
            for index, (brand, model) in enumerate(pairs):
                records.append({
                    "Marca": brand, "Modelo": model,
                    "Category General": "OTHERS",
                    "FAMILY": "DRILL",
                    "Auto Eligible": "SIM" if index == 9 else "NÃO",
                    "Resolution State": "PENDING",
                    "Review Required": "SIM",
                })
            pd.DataFrame(records).to_excel(
                output_file, index=False, sheet_name="Resultados")
            (root / "classification_01.log").write_text(
                "NIGHT_CLASSIFICATION_OUTPUT_ONLY_OK", encoding="utf-8")
            states = {model: ("RAW_EMPTY" if i == 9 else "RAW_OK")
                      for i, (_, model) in enumerate(pairs)}
            class FakeStore:
                def __init__(self, path):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def close(self):
                    pass
                def get(self, brand, model):
                    return SimpleNamespace(state=states[model])
            state = {"stages": {"classification": {"log": "classification_01.log"}}}
            isolated = {"files": {
                "input": str(input_file), "final": str(final_file),
                "output": str(output_file), "cache": str(cache_file),
            }, "_nightly_output_only": True}
            with patch.object(pp, "TavilyReadOnlyStore", FakeStore), \
                 patch.object(pp, "overnight_examine", return_value=([], {}, 10, 10)), \
                 patch.object(pp, "protected_originals", return_value=[]):
                self.assertTrue(pp.outcome_audit(root, state, isolated))
            quality = json.loads((root / "quality_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["status"], "PASS")
            self.assertEqual(quality["cohort"], 10)
            self.assertEqual(quality["useful_raw_count"], 9)
            self.assertEqual(quality["pending_raw_count"], 1)
            self.assertEqual(quality["accounted_cohort_count"], 10)
            self.assertEqual(quality["pending_raw_keys"], [pp.business_key("BRAND", "MODEL-9")])
            self.assertEqual(quality["result_integrity_errors"], [])

    def test_canonical_staging_keeps_no_raw_sku_pending(self):
        from shutil import copy2
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = {}
            staged = {}
            for name in ("master", "final"):
                old[name] = root / f"original_{name}.xlsx"
                staged[name] = root / f"staged_{name}.xlsx"
                pd.DataFrame(columns=["Marca", "Modelo"]).to_excel(old[name], index=False)
                copy2(old[name], staged[name])
            pairs = {
                pp.business_key("BRAND", "GOOD"): ("BRAND", "GOOD"),
                pp.business_key("BRAND", "EMPTY"): ("BRAND", "EMPTY"),
            }
            originals = {
                name: {"path": str(old[name])} for name in ("master", "final")
            }
            record = {"Marca": "BRAND", "Modelo": "GOOD",
                      "Category General": "OTHERS", "FAMILY": "DRILL",
                      "Resolution State": "RESOLVED"}
            with patch.object(pp, "_stage_only_business_columns_in_final",
                              return_value=len(pp.BASELINE_PUBLIC_COLUMNS)), \
                 patch.object(pp, "_write_final_sku_index_to_stage",
                              return_value={"status": "TEST"}):
                pp._night_stage_output_only_canonical(
                    originals, staged, pairs,
                    [("BRAND", "GOOD", {"results": []})],
                    [record],
                    SimpleNamespace(version="tests", hash="tests"))
            master = pd.read_excel(staged["master"], keep_default_na=False)
            empty = master.loc[master["Modelo"] == "EMPTY"].iloc[0]
            good = master.loc[master["Modelo"] == "GOOD"].iloc[0]
            self.assertEqual(empty["Status Processamento"], "PENDENTE")
            self.assertEqual(empty["Status Coleta"], "SEM_EVIDENCIA")
            self.assertEqual(empty["Reason Code"], "NIGHTLY_NO_USABLE_RAW")
            self.assertEqual(empty["Category General"], "")
            self.assertEqual(empty["FAMILY"], "")
            self.assertEqual(empty["Auto Eligible"], "NÃO")
            self.assertEqual(good["FAMILY"], "DRILL")

    def test_all_no_evidence_exports_valid_empty_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "classification_output.xlsx"
            pp.export_excel([], output, {"products": 0})
            frame = pd.read_excel(
                output, sheet_name="Resultados", keep_default_na=False)
            self.assertEqual(list(frame.columns), ["Marca", "Modelo"])
            self.assertEqual(len(frame), 0)

    def test_no_evidence_cannot_be_approved_at_promotion(self):
        code = (Path(__file__).resolve().parents[1] /
                "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        self.assertIn("if key in pending_keys:", code)
        self.assertIn('raise RuntimeError("PROMOTION_PENDING_KEY_OUTSIDE_COHORT")', code)
        self.assertIn('"Reason Code": "NIGHTLY_NO_USABLE_RAW"', code)
        self.assertIn('"pending_raw": quality.get("pending_raw_count", 0)', code)

if __name__ == "__main__":
    unittest.main()
