"""Regression: same-workbook normalized SKU lookup and physical coverage."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json

from openpyxl import Workbook, load_workbook
import pipeline_produtos as p


class FinalSkuIndexTests(unittest.TestCase):
    def make_final(self, path):
        wb = Workbook()
        sheet = wb.active
        sheet.title = "Base Final"
        sheet.append(p.BASELINE_PUBLIC_COLUMNS)
        rows = [
            ["FIXTEC", "FCD12L06 UL", "DRILL", "BATTERY", "OTHERS",
             "DRILLERS", "Cordless drill", "DOUBLE_CHECK", "Script", "PROVÁVEL", ""],
            ["FLEX", "PE 14 2 150 230/CEE", "POLISHER", "CORDED", "OTHERS",
             "POLISHER", "Electric polisher", "DOUBLE_CHECK", "Script", "PROVÁVEL", ""],
            ["FMWPOWER", "MCWG 100", "", "", "", "", "",
             "REVISAR", "Script", "REVISAR", ""],
            ["FOTON", "5417272", "", "", "", "", "",
             "REVISAR", "Script", "REVISAR", ""],
        ]
        for row in rows:
            sheet.append(row)
        wb.save(path)
        wb.close()

    def test_streaming_reader_does_not_use_random_access(self):
        # A read-only Openpyxl .cell(i,j) may rescan worksheet XML every time.
        # Regression: prohibit cell() and __getitem__ in the index path.
        class StreamingSheet:
            def __init__(self):
                self.columns = list(p.BASELINE_PUBLIC_COLUMNS)
            def iter_rows(self, values_only=False):
                if not values_only:
                    raise AssertionError("index must use values_only")
                yield tuple(self.columns)
                yield ("FIXTEC", "FCD12L06 UL", "DRILL", "BATTERY",
                       "OTHERS", "DRILLERS", "Cordless drill", "DOUBLE_CHECK",
                       "Script", "PROVÁVEL", None)
                yield ("FOTON", "0501.218.540", None, None, None, None,
                       None, "REVISAR", "Script", "REVISAR", None)
            def cell(self, *_args, **_kwargs):
                raise AssertionError("random cell access forbidden")
            def __getitem__(self, _key):
                raise AssertionError("worksheet slicing forbidden")
        rows, stats = p._final_sku_lookup_rows(StreamingSheet())
        self.assertEqual(stats["physical_rows"], 2)
        self.assertEqual(stats["normalized_keys"], 2)
        by_key = {r[0]: r for r in rows}
        self.assertEqual(by_key["fixtec|fcd12l06ul"][1],
                         "FIXTEC|FCD12L06 UL")
        self.assertEqual(by_key["foton|0501218540"][1],
                         "FOTON|0501.218.540")


    def test_normalized_index_maps_to_original_without_adding_public_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "Base Final Produtos Enriquecida.xlsx"
            self.make_final(final)
            campaign = {"fixtec|fcd12l06ul", "flex|pe142150230cee",
                        "fmwpower|mcwg100", "foton|5417272"}
            _, before = p._final_sku_index_state(final, campaign)
            self.assertEqual(before["status"], "INDEX_REQUIRED")
            self.assertEqual(before["campaign_keys_found"], 4)
            self.assertEqual(before["campaign_keys_with_original_format_difference"], 3)
            p._write_final_sku_index_to_stage(final)
            rows, after = p._final_sku_index_state(final, campaign)
            self.assertEqual(after["status"], "INDEX_ALIGNED")
            self.assertEqual(after["index_rows_expected"], 4)
            indexed = {row[0]: row[1] for row in rows}
            self.assertEqual(indexed["fixtec|fcd12l06ul"], "FIXTEC|FCD12L06 UL")
            self.assertEqual(indexed["foton|5417272"], "FOTON|5417272")
            wb = load_workbook(final, read_only=False)
            self.assertEqual(wb[p.FINAL_SKU_INDEX_SHEET].sheet_state, "hidden")
            self.assertEqual(
                [c.value for c in wb["Base Final"][1]], p.BASELINE_PUBLIC_COLUMNS)
            wb.close()

    def test_variant_collisions_are_marked_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "final.xlsx"
            self.make_final(final)
            wb = load_workbook(final)
            ws = wb["Base Final"]
            ws.append(["FIXTEC", "FCD12L06UL", "ENGINE", "PETROL", "OTHERS",
                       "ENGINE", "Other", "REVISAR", "Script", "REVISAR", ""])
            wb.save(final)
            wb.close()
            rows, report = p._final_sku_index_state(
                final, {"fixtec|fcd12l06ul"})
            self.assertEqual(report["ambiguous_keys"], 1)
            self.assertEqual(report["campaign_ambiguous_keys"], ["fixtec|fcd12l06ul"])
            self.assertEqual(rows[0][6], "AMBIGUO:VARIANTES_OU_CLASSIFICACAO")
            self.assertEqual(rows[0][1], "")

    def test_transactional_index_has_backup_and_validates_physical_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            final = root / "final.xlsx"
            self.make_final(final)
            campaign = root / "piloto_argentina" / "controlled_night" / "campaigns" / "c1"
            campaign.mkdir(parents=True)
            (campaign / "campaign.json").write_text(
                json.dumps({"status":"COMPLETE","promotion":{"status":"COMMITTED"},
                            "batches":[{"cohort_keys":["fixtec|fcd12l06ul",
                                                       "fmwpower|mcwg100"]}]}),
                encoding="utf-8")
            cfg = {"files":{"final":str(final),"baseline_backup_dir":str(root/"backups")}}
            with patch.object(p, "OUT", root/"piloto_argentina"/"controlled_night"), patch.object(p, "ROOT", root):
                first = p.reconcile_final_sku_index(cfg)
                self.assertEqual(first["campaign_keys_found"], 2)
                self.assertEqual(first["status"], "INDEX_REQUIRED")
                done = p.reconcile_final_sku_index(cfg, commit=True)
                self.assertEqual(done["status"], "INDEX_COMMITTED")
                self.assertTrue(Path(done["backup"]).exists())
                repeat = p.reconcile_final_sku_index(cfg)
                self.assertEqual(repeat["status"], "INDEX_ALIGNED")
                wb = load_workbook(final)
                self.assertEqual(len(wb["Base Final"]["A"]), 5)
                wb.close()


if __name__ == "__main__":
    unittest.main()
