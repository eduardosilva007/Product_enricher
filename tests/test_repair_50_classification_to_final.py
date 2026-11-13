"""Offline regression: 24 stored outputs are projected into the official Excel only."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from scripts.repair_50_classification_to_final import repair


HEADER = ["Marca", "Modelo", "O que é (Tipo)", "Alimentação", "Category General",
          "FAMILY", "Título Real", "Próxima Ação", "Status", "Revisão", "Revisão B.I"]


def write(path, sheet, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(HEADER)
    for rec in records:
        ws.append([rec.get(x) for x in HEADER])
    wb.save(path)
    wb.close()


class OfflineTwentyFourReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.campaign_id = "20261113T124106Z_2d6e7cfe"
        self.campaign = self.root / "piloto_argentina" / "controlled_night" / "campaigns" / self.campaign_id
        self.campaign.mkdir(parents=True)
        files = {
            "final": "Base Final Produtos Enriquecida.xlsx",
            "master": "Consolidado Base de Produtos Enriquecida.xlsx",
            "classification_db": "pipeline_v3.sqlite",
        }
        (self.root / "pipeline_config.json").write_text(
            json.dumps({"files": files}), encoding="utf-8")
        self.final = self.root / files["final"]
        self.master = self.root / files["master"]
        self.db = self.root / files["classification_db"]
        sqlite3.connect(self.db).close()
        self.records = [{"Marca": "FIXTEC", "Modelo": f"SKU{i:03}"}
                        for i in range(50)]
        self.records.append({"Marca": "OUTSIDE", "Modelo": "HUMAN",
                             "O que é (Tipo)": "ENGINE", "Category General": "OTHERS",
                             "FAMILY": "ENGINE", "Revisão B.I": "OK", "Status": "Manual"})
        write(self.final, "Base Final", self.records)
        write(self.master, "Consolidado", self.records)
        batches = []
        for run_idx, begin in enumerate((0, 20, 40)):
            run = self.root / "piloto_argentina" / "controlled_night" / f"run{run_idx}"
            run.mkdir()
            end = min(begin + 20, 50)
            selected = self.records[begin:end]
            input_path, output_path = run / "raw_empty_cohort.xlsx", run / "classification_output.xlsx"
            write(input_path, "Sheet1", selected)
            output = []
            for i in range(begin, min(end, 24)):
                item = dict(self.records[i])
                if i < 20:
                    item.update({"O que é (Tipo)": "DRILL", "Alimentação": "BATTERY",
                                 "Category General": "OTHERS", "FAMILY": "DRILL",
                                 "Título Real": f"Drill {i}", "Próxima Ação": "DOUBLE_CHECK",
                                 "Status": "Script", "Revisão": "PROVÁVEL"})
                else:
                    item["Título Real"] = f"Unknown item {i}"
                output.append(item)
            write(output_path, "Resultados", output)
            (run / "isolated_config.json").write_text(
                json.dumps({"files": {"input": str(input_path), "output": str(output_path)}}),
                encoding="utf-8")
            batches.append({
                "run_dir": str(run),
                "cohort_keys": [f"fixtec|sku{i:03}" for i in range(begin, end)],
            })
        state = {
            "status": "COMPLETE", "audited_products": 50, "batches": batches,
            "promotion": {"status": "COMMITTED", "classification_rows": 24,
                          "without_classification_output": 26}
        }
        (self.campaign / "campaign.json").write_text(json.dumps(state), encoding="utf-8")

    def test_dry_run_then_apply_no_api_and_idempotent(self):
        before = self.final.read_bytes()
        preview = repair(self.root, self.campaign_id, 24, False)
        self.assertEqual(preview["requires_update"], 24)
        self.assertEqual(preview["source_complete"], 20)
        self.assertEqual(before, self.final.read_bytes())
        result = repair(self.root, self.campaign_id, 24, True)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["now_consistent"], 24)
        self.assertEqual(result["final_complete_of_50"], 20)
        self.assertEqual(result["sqlite_status"], "SQLITE_MERGED")
        self.assertTrue(Path(result["backup"]).is_file())
        book = load_workbook(self.final)
        try:
            ws = book["Base Final"]
            headers = {c.value: c.column for c in ws[1]}
            self.assertEqual(ws.cell(2, headers["FAMILY"]).value, "DRILL")
            self.assertEqual(ws.cell(25, headers["Título Real"]).value, "Unknown item 23")
            self.assertEqual(ws.cell(52, headers["Revisão B.I"]).value, "OK")
        finally:
            book.close()
        repeat = repair(self.root, self.campaign_id, 24, True)
        self.assertEqual(repeat["status"], "PASS")
        self.assertEqual(repeat["requires_update"], 0)

    def test_locked_existing_human_classification_is_not_modified(self):
        book = load_workbook(self.final)
        ws = book["Base Final"]
        headers = {c.value: c.column for c in ws[1]}
        ws.cell(2, headers["Status"]).value = "Manual"
        ws.cell(2, headers["O que é (Tipo)"]).value = "ENGINE"
        book.save(self.final)
        book.close()
        result = repair(self.root, self.campaign_id, 24, True)
        self.assertEqual(result["protected"], 1)
        book = load_workbook(self.final, read_only=True)
        try:
            ws = book["Base Final"]
            self.assertEqual(ws.cell(2, headers["O que é (Tipo)"]).value, "ENGINE")
        finally:
            book.close()

    def test_wrong_number_of_outputs_is_blocked_without_changes(self):
        before = self.final.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_RECORDED_RESULTS"):
            repair(self.root, self.campaign_id, 25, True)
        self.assertEqual(before, self.final.read_bytes())


if __name__ == "__main__":
    unittest.main()
