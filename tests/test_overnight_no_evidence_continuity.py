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

    def test_partial_classification_from_budget_cap_is_audited_with_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pairs = [("BRAND", f"MODEL-{i}") for i in range(10)]
            for name in ("input", "final"):
                pd.DataFrame(pairs, columns=["Marca", "Modelo"]).to_excel(
                    root / f"{name}.xlsx", index=False)
            pd.DataFrame([{
                "Marca": "BRAND", "Modelo": "MODEL-0",
                "Resolution State": "REVIEW", "Auto Eligible": "NÃO",
                "Review Required": "SIM",
            }, {
                "Marca": "BRAND", "Modelo": "MODEL-1",
                "Stage": "API_PAUSED", "Resolution State": "API_PAUSED",
                "Auto Eligible": "NÃO", "Review Required": "SIM",
            }]).to_excel(root / "output.xlsx", index=False, sheet_name="Resultados")
            (root / "classification_01.log").write_text(
                "NIGHT_CLASSIFICATION_OUTPUT_ONLY_OK\\n"
                "NIGHT_CLASSIFICATION_BUDGET_PENDING | reason=CLEF_LOCAL_TOKEN_CAP:",
                encoding="utf-8")
            class FakeStore:
                def __init__(self, path):
                    pass
                def close(self):
                    pass
                def get(self, brand, model):
                    return SimpleNamespace(state="RAW_OK")
            state = {"stages": {"classification": {"log": "classification_01.log"}}}
            isolated = {"files": {
                "input": str(root / "input.xlsx"),
                "final": str(root / "final.xlsx"),
                "output": str(root / "output.xlsx"),
                "cache": str(root / "cache.sqlite"),
            }, "_nightly_output_only": True}
            with patch.object(pp, "TavilyReadOnlyStore", FakeStore), \
                 patch.object(pp, "overnight_examine", return_value=([], {}, 10, 10)), \
                 patch.object(pp, "protected_originals", return_value=[]):
                self.assertTrue(pp.outcome_audit(root, state, isolated))
            quality = json.loads((root / "quality_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["status"], "PASS")
            self.assertEqual(quality["useful_raw_count"], 10)
            self.assertEqual(quality["pending_classification_count"], 9)
            self.assertTrue(quality["classification_budget_paused"])
            self.assertEqual(len(quality["pending_classification_keys"]), 9)
            self.assertIn(pp.business_key("BRAND", "MODEL-1"),
                          quality["pending_classification_keys"])
            self.assertEqual(quality["result_integrity_errors"], [])

    def test_budget_cap_continuation_is_scoped_to_isolated_mode(self):
        code = (Path(__file__).resolve().parents[1] /
                "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        self.assertIn("if output_only and budget_pause:", code)
        self.assertIn("OFFLINE_BOUNDED_BUDGET_PARTIAL_AUDIT", code)
        self.assertIn('raise RuntimeError("RECOVERY_BATCH_PAUSED: " + paused_reason)', code)
        self.assertIn('"JEV_INCOMPLETE_REQUIRES_AUDIT: automatic classification resume disabled;', code)
        self.assertIn('if stages.get("classification",{}).get("attempts",0)>0:', code)

    def test_promotion_quarantines_identity_and_budget_pending(self):
        import ast
        code = (Path(__file__).resolve().parents[1] /
                "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(code)
        promotion = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef)
                         and node.name == "overnight_promote_campaign")
        source = ast.get_source_segment(code, promotion)
        self.assertIn(
            'pending_classification_keys = set(', source)
        self.assertIn(
            'quarantined_keys = pending_keys | identity_review_keys | pending_classification_keys',
            source)
        self.assertLess(source.index('pending_keys = set('),
                        source.index('isolated_databases.append(('))
        self.assertLess(source.index('identity_review_keys = set('),
                        source.index('isolated_databases.append(('))
        self.assertIn('set(cohort_keys) - quarantined_keys', source)
        self.assertIn('if key in quarantined_keys:', source)
        self.assertLess(source.index('if key in quarantined_keys:'),
                        source.index('identity_errors = _overnight_auto_title_identity_errors('))

    def test_title_identity_mismatch_is_quarantined_not_campaign_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pairs = [("LAIBRIL", "DC_1000/E")] + [
                ("BRAND", f"MODEL-{i}") for i in range(9)]
            input_file = root / "input.xlsx"
            final_file = root / "final.xlsx"
            output_file = root / "output.xlsx"
            pd.DataFrame(pairs, columns=["Marca", "Modelo"]).to_excel(
                input_file, index=False)
            pd.DataFrame(pairs, columns=["Marca", "Modelo"]).to_excel(
                final_file, index=False)
            pd.DataFrame([{
                "Marca": "LAIBRIL", "Modelo": "DC_1000/E",
                "Título Real": "Different product model 111",
                "O que é (Tipo)": "DRILL", "Category General": "OTHERS",
                "FAMILY": "DRILL", "Resolution State": "RESOLVED",
                "Auto Eligible": "SIM", "Review Required": "NÃO",
                "Classification Identity": "STRONG",
            }]).to_excel(output_file, index=False, sheet_name="Resultados")
            (root / "classification_01.log").write_text(
                "NIGHT_CLASSIFICATION_OUTPUT_ONLY_OK", encoding="utf-8")
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
                    return SimpleNamespace(state="RAW_OK")
            state = {"stages": {"classification": {"log": "classification_01.log"}}}
            isolated = {"files": {
                "input": str(input_file), "final": str(final_file),
                "output": str(output_file), "cache": str(root / "cache.sqlite"),
            }, "_nightly_output_only": True}
            with patch.object(pp, "TavilyReadOnlyStore", FakeStore), \
                 patch.object(pp, "overnight_examine", return_value=([], {}, 10, 10)), \
                 patch.object(pp, "protected_originals", return_value=[]):
                self.assertTrue(pp.outcome_audit(root, state, isolated))
            quality = json.loads((root / "quality_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["useful_raw_count"], 10)
            self.assertEqual(quality["identity_review_count"], 1)
            self.assertEqual(quality["identity_review_keys"],
                             [pp.business_key("LAIBRIL", "DC_1000/E")])
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
                pd.DataFrame(columns=pp.BASELINE_PUBLIC_COLUMNS).to_excel(old[name], index=False)
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
            with patch.object(pp, "_write_final_sku_index_to_stage",
                              return_value={"status": "TEST"}):
                pp._night_stage_output_only_canonical(
                    originals, staged, pairs,
                    [("BRAND", "GOOD", {"results": []})],
                    [record],
                    SimpleNamespace(version="tests", hash="tests"))
            self.assertEqual(staged["master"].read_bytes(), old["master"].read_bytes())
            final = pd.read_excel(staged["final"], keep_default_na=False)
            empty = final.loc[final["Modelo"] == "EMPTY"].iloc[0]
            good = final.loc[final["Modelo"] == "GOOD"].iloc[0]
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
        self.assertIn("if key in quarantined_keys:", code)
        self.assertIn("quarantined_keys = pending_keys | identity_review_keys | pending_classification_keys", code)
        self.assertIn('raise RuntimeError("PROMOTION_REVIEW_KEY_OUTSIDE_COHORT")', code)
        self.assertIn('"Reason Code": "NIGHTLY_NO_USABLE_RAW"', code)
        self.assertIn('"pending_raw": quality.get("pending_raw_count", 0)', code)

if __name__ == "__main__":
    unittest.main()
