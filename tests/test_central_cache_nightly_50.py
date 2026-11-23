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
            # Final is the ONLY canonical workbook; Consolidado stays byte-identical.
            self.assertEqual(core.file_hash(staged["master"]), core.file_hash(Path(originals["master"]["path"])))
            after, _ = core._read_business_sheet(staged["final"])
            self.assertEqual(len(after), 51)  # 3 original + 48 new
            self.assertEqual(after.iloc[0]["Revisão B.I"], "OK")
            self.assertEqual(after.iloc[0]["FAMILY"], "DRILLERS")
            self.assertEqual(after.iloc[1]["FAMILY"], "ENGINE")
            self.assertTrue(set(core.BASELINE_PUBLIC_COLUMNS).issubset(after.columns))
            classified = after.loc[(after["Marca"] == "BRAND")
                                   & (after["Modelo"] == "M000")].iloc[0]
            self.assertEqual(classified["O que é (Tipo)"], "DRILL")
            # Operational fields live in Final; nothing is auto-approved.
            self.assertEqual(classified["Review Required"], "SIM")
            self.assertEqual(classified["Auto Eligible"], "NÃO")
            self.assertEqual(classified["Revisão"], "PROVÁVEL")
            self.assertTrue(pd.isna(classified["Revisão B.I"]))
            pending = after.loc[(after["Marca"] == "BRAND")
                                & (after["Modelo"] == "M047")].iloc[0]
            self.assertEqual(pending["Status Classificação"], "PENDENTE_CLASSIFICACAO")
            self.assertEqual(pending["Raw State"], "RAW_OK")
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
            self.assertEqual(core.file_hash(staged["master"]), core.file_hash(Path(originals["master"]["path"])))
            after, _ = core._read_business_sheet(staged["final"])
            self.assertTrue(pd.isna(after.iloc[2]["O que é (Tipo)"]))
            self.assertTrue(pd.isna(after.iloc[2]["Category General"]))
            self.assertTrue(pd.isna(after.iloc[2]["FAMILY"]))
            self.assertEqual(after.iloc[0]["Revisão B.I"], "OK")
            self.assertEqual(after.iloc[1]["FAMILY"], "ENGINE")
            self.assertEqual(after.iloc[2]["Review Required"], "SIM")
            self.assertEqual(after.iloc[2]["Revisão"], "REVISAR")


    def test_stage_preserves_all_final_columns_and_leaves_master_untouched(self):
        """Final-only architecture: Final is never collapsed to the 11 public
        columns (operational/audit fields are kept) and Consolidado is unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            originals, staged = self.make_baselines(tmp)
            pairs = {"fixtec|fcd12l06ul": ("FIXTEC", "FCD12L06UL")}
            raw = [("FIXTEC", "FCD12L06UL", {"results": []})]
            core._night_stage_output_only_canonical(
                originals, staged, pairs, raw, [],
                SimpleNamespace(version="v", hash="h"))
            before_final, _ = core._read_business_sheet(Path(originals["final"]["path"]))
            after_final, _ = core._read_business_sheet(staged["final"])
            self.assertTrue(set(before_final.columns).issubset(after_final.columns))
            self.assertTrue(set(core.BASELINE_PUBLIC_COLUMNS).issubset(after_final.columns))
            for column in ("Raw State", "Review Required", "Status Classificação"):
                self.assertIn(column, after_final.columns)
            self.assertEqual(after_final.iloc[0]["Revisão B.I"], "OK")
            self.assertEqual(core.file_hash(staged["master"]),
                             core.file_hash(Path(originals["master"]["path"])))

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
