"""Regression: scraped evidence with Excel-forbidden control chars must not crash reports."""
import tempfile
import unittest
from pathlib import Path
from openpyxl import load_workbook
from pipeline_produtos import _excel_export_safe_value, export_excel


class ExcelEvidenceExportRegressions(unittest.TestCase):
    def test_sanitize_only_forbidden_control_characters(self):
        raw = "DCD7771D2-AR\\x07 text\\x0b tab\\t newline\\n accentuação"
        value=_excel_export_safe_value(raw)
        self.assertNotIn("\x07",value)
        self.assertNotIn("\x0b",value)
        self.assertIn("\t",value)
        self.assertIn("\n",value)
        self.assertIn("accentuação",value)
        self.assertEqual(_excel_export_safe_value(13),13)

    def test_report_exports_summary_rows_and_review_without_original_mutation(self):
        dirty="DEWALT DCD7771D2-AR\x07 source\x01 evidence"
        rows=[
            {"Marca":"DEWALT","Modelo":"DCD7771D2-AR",
             "Raw Search Results":dirty,"Review Required":"SIM",
             "Confidence":0.75},
        ]
        summary={"evidence":dirty,"count":1}
        with tempfile.TemporaryDirectory() as path:
            target=Path(path)/"results.xlsx"
            export_excel(rows,target,summary)
            self.assertTrue(target.is_file())
            book=load_workbook(target,read_only=True,data_only=True)
            try:
                self.assertEqual(set(book.sheetnames),{"Resumo","Resultados","Revisar"})
                self.assertEqual(book["Resultados"]["D2"].value,"SIM")
                self.assertNotIn("\x07",book["Resultados"]["C2"].value)
                self.assertNotIn("\x01",book["Revisar"]["C2"].value)
                self.assertEqual(book["Resultados"]["E2"].value,0.75)
            finally:
                book.close()
            self.assertEqual(rows[0]["Raw Search Results"],dirty)
            self.assertEqual(summary["evidence"],dirty)

if __name__=="__main__":unittest.main()
