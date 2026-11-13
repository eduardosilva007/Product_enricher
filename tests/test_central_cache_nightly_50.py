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
                if name == "master":
                    self.assertEqual(classified["Review Required"], "SIM")
                    self.assertEqual(classified["Auto Eligible"], "NÃO")
                else:
                    self.assertEqual(list(after.columns), core.BASELINE_PUBLIC_COLUMNS)
                    self.assertEqual(classified["Revisão"], "PROVÁVEL")
                self.assertTrue(pd.isna(classified["Revisão B.I"]))
                pending = after.loc[(after["Marca"] == "BRAND")
                                    & (after["Modelo"] == "M047")].iloc[0]
                if name == "master":
                    self.assertEqual(pending["Status Classificação"], "PENDENTE_CLASSIFICACAO")
                    self.assertEqual(pending["Raw State"], "RAW_OK")
                else:
                    self.assertEqual(pending["Revisão"], "REVISAR")
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


    def test_missing_classification_clears_only_existing_unapproved_taxonomy(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as tmp:
            originals, staged = self.make_baselines(tmp)
            for name in ("master", "final"):
                workbook = load_workbook(originals[name]["path"])
                sheet = workbook.active
                headers = {cell.value: cell.column for cell in sheet[1]}
                for field, value in (
                    ("O que é (Tipo)", "WRONG TYPE"),
                    ("Category General", "OTHERS"),
                    ("FAMILY", "ENGINE"),
                ):
                    sheet.cell(4, headers[field]).value = value
                workbook.save(originals[name]["path"])
                workbook.close()
                shutil.copy2(originals[name]["path"], staged[name])
            pairs = {"fixtec|fcd12l06ul": ("FIXTEC", "FCD12L06UL")}
            raw = [("FIXTEC", "FCD12L06UL", {"results": []})]
            core._night_stage_output_only_canonical(
                originals, staged, pairs, raw, [],
                SimpleNamespace(version="v", hash="h"))
            for name in ("master", "final"):
                after, _ = core._read_business_sheet(staged[name])
                self.assertTrue(pd.isna(after.iloc[2]["O que é (Tipo)"]))
                self.assertTrue(pd.isna(after.iloc[2]["Category General"]))
                self.assertTrue(pd.isna(after.iloc[2]["FAMILY"]))
                self.assertEqual(after.iloc[0]["Revisão B.I"], "OK")
                self.assertEqual(after.iloc[1]["FAMILY"], "ENGINE")
                if name == "master":
                    self.assertEqual(after.iloc[2]["Review Required"], "SIM")
                else:
                    self.assertEqual(list(after.columns), core.BASELINE_PUBLIC_COLUMNS)
                    self.assertEqual(after.iloc[2]["Revisão"], "REVISAR")


    def test_stage_removes_diagnostics_only_in_final(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as tmp:
            originals, staged = self.make_baselines(tmp)
            pairs = {"fixtec|fcd12l06ul": ("FIXTEC", "FCD12L06UL")}
            raw = [("FIXTEC", "FCD12L06UL", {"results": []})]
            core._night_stage_output_only_canonical(
                originals, staged, pairs, raw, [],
                SimpleNamespace(version="v", hash="h"))
            after_final, _ = core._read_business_sheet(staged["final"])
            after_master, _ = core._read_business_sheet(staged["master"])
            self.assertEqual(list(after_final.columns), core.BASELINE_PUBLIC_COLUMNS)
            self.assertIn("Raw State", after_master.columns)
            self.assertIn("Review Required", after_master.columns)
            self.assertNotIn("Raw State", after_final.columns)
            self.assertEqual(after_final.iloc[0]["Revisão B.I"], "OK")

    def test_cleanup_menu_instructions_and_guard(self):
        import inspect
        menu_source = inspect.getsource(core.interactive_menu)
        main_source = inspect.getsource(core.main)
        self.assertIn("10 - LIMPAR BASE FINAL", menu_source)
        self.assertIn("--final-schema-interactive", menu_source)
        self.assertIn('input("Remover SOMENTE colunas tecnicas', main_source)
        self.assertIn("commit=False", main_source)
        self.assertIn("commit=True", main_source)

    def test_promotion_contains_post_write_cache_and_excel_audits(self):
        import inspect
        source = inspect.getsource(core.overnight_promote_campaign)
        self.assertIn("CENTRALIZE_POSTWRITE_EXCEL_MISSING", source)
        self.assertIn("CENTRALIZE_POSTWRITE_CACHE_MISSING", source)
        self.assertIn("post_commit_integrity", source)

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
