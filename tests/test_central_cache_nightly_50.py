"""No-API regression for exact-row promotion of complete output-only campaigns."""
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pipeline_produtos as core


class CentralCacheProjectionTests(unittest.TestCase):
    def make_baselines(self, folder):
        rows = [
            {"Marca": "OLD", "Modelo": "LOCKED", "O que é (Tipo)": "DRILL",
             "Category General": "OTHERS", "FAMILY": "DRILLERS",
             "Status": "Manual", "Revisão B.I": "OK", "Revisão": "VALIDADO_MANUAL"},
            {"Marca": "OTHER", "Modelo": "AB", "O que é (Tipo)": "ENGINE",
             "Category General": "OTHERS", "FAMILY": "ENGINE", "Status": "Script",
             "Revisão": "PROVÁVEL"},
            {"Marca": "FIXTEC", "Modelo": "FCD12L06UL", "Status": "Script",
             "Revisão": "REVISAR"},
        ]
        originals, staged = {}, {}
        for name in ("master", "final"):
            path = Path(folder) / (name + ".xlsx")
            pd.DataFrame(rows).to_excel(
                path, sheet_name="Consolidado" if name == "master" else "Base Final",
                index=False)
            stage = Path(folder) / ("stage_" + name + ".xlsx")
            shutil.copy2(path, stage)
            originals[name] = {"path": str(path)}
            staged[name] = stage
        return originals, staged

    def test_50_registered_24_results_26_pending_no_bi_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            originals, staged = self.make_baselines(tmp)
            pairs = {"fixtec|fcd12l06ul": ("FIXTEC", "FCD12L06UL"),
                     "old|locked": ("OLD", "LOCKED")}
            pairs.update({f"brand|m{i:03d}": ("BRAND", f"M{i:03d}") for i in range(48)})
            raw = [(b, m, {"results": [],
                           "_collection": {"state": "NO_TARGET_EVIDENCE"}})
                   for b, m in pairs.values()]
            records = [{"Marca": "FIXTEC", "Modelo": "FCD12L06UL",
                        "O que é (Tipo)": "DRILL", "Category General": "OTHERS",
                        "FAMILY": "DRILLERS", "Resolution State": "CLASSIFIED_CLEF"}]
            records += [{"Marca": "BRAND", "Modelo": f"M{i:03d}",
                         "O que é (Tipo)": "DRILL", "Category General": "OTHERS",
                         "FAMILY": "DRILLERS", "Resolution State": "RESOLVED",
                         "Auto Eligible": "SIM", "Review Required": "NÃO",
                         "Classification Identity": "STRONG"}
                        for i in range(23)]
            result = core._night_stage_output_only_canonical(
                originals, staged, pairs, raw, records,
                SimpleNamespace(version="test", hash="hash"))
            self.assertEqual((result["cohort"], result["classification_rows"],
                              result["pending_classification"]), (50, 24, 26))
            for name in ("master", "final"):
                before, _ = core._read_business_sheet(Path(originals[name]["path"]))
                after, _ = core._read_business_sheet(staged[name])
                self.assertEqual(len(after), 51)  # 3 original + 48 new
                self.assertEqual(after.iloc[0]["Revisão B.I"], "OK")
                self.assertEqual(after.iloc[0]["FAMILY"], "DRILLERS")
                self.assertEqual(after.iloc[1]["FAMILY"], "ENGINE")
                classified = after.loc[(after["Marca"] == "BRAND")
                                       & (after["Modelo"] == "M000")].iloc[0]
                self.assertEqual(classified["O que é (Tipo)"], "DRILL")
                self.assertEqual(classified["Review Required"], "SIM")
                self.assertEqual(classified["Auto Eligible"], "NÃO")
                self.assertTrue(pd.isna(classified["Revisão B.I"]))
                pending = after.loc[(after["Marca"] == "BRAND")
                                    & (after["Modelo"] == "M047")].iloc[0]
                self.assertEqual(pending["Status Classificação"], "PENDENTE_CLASSIFICACAO")
                self.assertEqual(pending["Raw State"], "RAW_OK")
                self.assertTrue(pd.isna(pending["Category General"]))
                self.assertTrue(pd.isna(pending["Revisão B.I"]))

    def test_normalized_alias_collision_blocks_before_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            originals, staged = self.make_baselines(tmp)
            pairs = {"fixtec|fcd12l06ul": ("FIXTEC", "FCD-12L06UL")}
            raw = [("FIXTEC", "FCD-12L06UL", {"results": []})]
            with self.assertRaisesRegex(RuntimeError, "NORMALIZED_ALIAS_COLLISION"):
                core._night_stage_output_only_canonical(
                    originals, staged, pairs, raw, [],
                    SimpleNamespace(version="v", hash="h"))

    def test_extra_classifier_output_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            originals, staged = self.make_baselines(tmp)
            pairs = {"fixtec|fcd12l06ul": ("FIXTEC", "FCD12L06UL")}
            raw = [("FIXTEC", "FCD12L06UL", {"results": []})]
            with self.assertRaisesRegex(RuntimeError, "INVALID_PROPOSAL"):
                core._night_stage_output_only_canonical(
                    originals, staged, pairs, raw,
                    [{"Marca": "OTHER", "Modelo": "UNRELATED"}],
                    SimpleNamespace(version="v", hash="h"))

    def test_menu_has_offline_centralization_and_explicit_confirmation(self):
        parser = core.build_parser()
        self.assertEqual(parser.parse_args(["--mode", "overnight-centralize"]).mode,
                         "overnight-centralize")
        import inspect
        source = inspect.getsource(core.main)
        self.assertIn("GRAVAR", source)
        self.assertIn("dry_run=True", source)
        self.assertIn("CACHE CENTRAL PENDENTE", source)


if __name__ == "__main__":
    unittest.main()
