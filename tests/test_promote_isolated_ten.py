"""Contract tests for native, ten-SKU, offline official promotion."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline_produtos import promote_isolated_ten

FIELDS = ["Marca","Modelo","O que é (Tipo)","Alimentação","Category General",
          "FAMILY","Título Real","Status","Revisão B.I","Resolution State",
          "Review Required","Auto Eligible","Classification Outcome"]

def make_book(path, rows, sheet="Base Final"):
    wb=Workbook()
    ws=wb.active
    ws.title=sheet
    ws.append(FIELDS)
    for row in rows: ws.append([row.get(c) for c in FIELDS])
    wb.save(path)
    wb.close()

def case(i, *, valid=True):
    return {"Marca":"DCA","Modelo":f"SKU-{i:02d}",
            "O que é (Tipo)":"DRILL","Alimentação":"CORDED",
            "Category General":"OTHERS","FAMILY":"POWER TOOLS",
            "Título Real":f"DCA SKU-{i:02d} Drill",
            "Status":"Script","Resolution State":"RESOLVED",
            "Review Required":"NÃO" if valid else "SIM",
            "Auto Eligible":"SIM" if valid else "NÃO",
            "Classification Outcome":"RULE_CLASSIFIED"}

class PromoteIsolatedTenTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name)
        self.run=self.root/"controlled_night"/"pilot"
        self.run.mkdir(parents=True)
        self.source=self.run/"final.xlsx"
        self.official=self.root/"official.xlsx"
        self.input=self.run/"input.xlsx"
        self.output=self.run/"classification_output.xlsx"
        self.records=[case(i) for i in range(10)]
        make_book(self.source,self.records)
        make_book(self.input,[{"Marca":x["Marca"],"Modelo":x["Modelo"]} for x in self.records])
        make_book(self.output,self.records,sheet="Resultados")
        other={"Marca":"OTHER","Modelo":"KEEP-001","O que é (Tipo)":"PART",
               "Alimentação":"UNKNOWN","Category General":"OTHERS",
               "FAMILY":"SPARE PART","Status":"Manual","Revisão B.I":"OK"}
        make_book(self.official,
                  [{**x,"O que é (Tipo)":"REVISAR","Alimentação":"UNKNOWN",
                    "Category General":None,"FAMILY":None} for x in self.records] + [other])
        (self.run/"isolated_config.json").write_text(json.dumps({
            "files":{"final":str(self.source),"input":str(self.input)}
        }),encoding="utf-8")

    def test_dryrun_then_commit_exact_ten_and_backup(self):
        before=self.official.read_bytes()
        dry=promote_isolated_ten(self.run,self.official,commit=False)
        self.assertEqual(dry["status"],"DRY_RUN_PASS")
        self.assertEqual(self.official.read_bytes(),before)
        report=promote_isolated_ten(self.run,self.official,commit=True)
        self.assertEqual(report["status"],"COMMITTED")
        self.assertEqual(report["cohort"],10)
        self.assertTrue(Path(report["backup"]).exists())
        self.assertEqual(Path(report["backup"]).read_bytes(),before)
        wb=load_workbook(self.official)
        ws=wb.active
        self.assertEqual(ws.cell(2,3).value,"DRILL")
        self.assertEqual(ws.cell(11,6).value,"POWER TOOLS")
        self.assertEqual(ws.cell(12,2).value,"KEEP-001")
        self.assertEqual(ws.cell(12,6).value,"SPARE PART")
        self.assertEqual(ws.max_row,12)
        wb.close()

    def test_manual_or_bi_lock_blocks_all_changes(self):
        wb=load_workbook(self.official)
        ws=wb.active
        ws.cell(2,8,"Manual")
        wb.save(self.official)
        wb.close()
        before=self.official.read_bytes()
        with self.assertRaisesRegex(RuntimeError,"PROMOTE10_HUMAN_LOCK"):
            promote_isolated_ten(self.run,self.official,commit=True)
        self.assertEqual(self.official.read_bytes(),before)

    def test_unrelated_duplicate_in_final_is_ignored(self):
        wb=load_workbook(self.official)
        ws=wb.active
        ws.append([ws.cell(12,col).value for col in range(1,14)])
        wb.save(self.official)
        wb.close()
        report=promote_isolated_ten(self.run,self.official,commit=True)
        self.assertEqual(report["status"],"COMMITTED")
        wb=load_workbook(self.official)
        self.assertEqual(wb.active.max_row,13)
        self.assertEqual(wb.active.cell(12,2).value,"KEEP-001")
        self.assertEqual(wb.active.cell(13,2).value,"KEEP-001")
        wb.close()

    def test_duplicate_target_blocks(self):
        wb=load_workbook(self.official)
        ws=wb.active
        ws.append([ws.cell(2,col).value for col in range(1,14)])
        wb.save(self.official)
        wb.close()
        before=self.official.read_bytes()
        with self.assertRaisesRegex(RuntimeError,"PROMOTE10_DUPLICATE_BUSINESS_KEY"):
            promote_isolated_ten(self.run,self.official,commit=True)
        self.assertEqual(self.official.read_bytes(),before)

    def test_incomplete_result_blocks(self):
        wb=load_workbook(self.output)
        wb.active.cell(2,11,"SIM")
        wb.save(self.output)
        wb.close()
        before=self.official.read_bytes()
        with self.assertRaisesRegex(RuntimeError,"PROMOTE10_NOT_ELIGIBLE"):
            promote_isolated_ten(self.run,self.official,commit=True)
        self.assertEqual(self.official.read_bytes(),before)

if __name__=="__main__":
    unittest.main()
