"""CI regression for cumulative Base Final gate; tests real XLSX public interface."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import Workbook
from tools.final_output_gate import validate


def write(path, columns, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(columns)
    for row in rows:
        ws.append(row)
    wb.save(path)
    wb.close()


class CumulativeFinalOutputTests(unittest.TestCase):
    def test_public_final_contains_unrelated_history_and_duplicate_input_rows(self):
        with TemporaryDirectory() as d:
            src, target = Path(d) / "input.xlsx", Path(d) / "final.xlsx"
            write(src, ["Marca", "Modelo"],
                  [["STIHL", "FS120"], ["STIHL", "FS120"], ["HONDA", "GX"]])
            columns = ["Marca", "Modelo", "O que é (Tipo)", "Alimentação",
                       "Category General", "FAMILY", "Título Real", "Próxima Ação",
                       "Status", "Revisão", "Revisão B.I"]
            write(target, columns, [
                ["OLD", "OLD1", "DRILL", "BATTERY", "OTHERS", "DRILLERS",
                 "Old drill", "", "Manual", "VALIDADO_MANUAL", "OK"],
                ["STIHL", "FS120", "BRUSHCUTTER", "PETROL", "PETROL HANDHELD",
                 "PETROL BRUSHCUTTER", "STIHL FS120", "", "Script", "PROVÁVEL", ""],
                ["STIHL", "FS120", "BRUSHCUTTER", "PETROL", "PETROL HANDHELD",
                 "PETROL BRUSHCUTTER", "STIHL FS120", "", "Script", "PROVÁVEL", ""],
                ["HONDA", "GX", "ENGINE", "PETROL", "OTHERS", "ENGINE",
                 "Honda GX", "", "Script", "PROVÁVEL", ""],
            ])
            errors, count_input, count_final = validate(src, target)
            self.assertEqual((count_input, count_final), (3, 4))
            self.assertEqual(errors, [])

    def test_missing_input_occurrence_still_fails_despite_historical_rows(self):
        with TemporaryDirectory() as d:
            src, target = Path(d) / "input.xlsx", Path(d) / "final.xlsx"
            write(src, ["Marca", "Modelo"],
                  [["STIHL", "FS120"], ["STIHL", "FS120"]])
            write(target, ["Marca", "Modelo", "Título Real"],
                  [["STIHL", "FS120", "One"], ["OLD", "OLD1", "Historical"]])
            errors, _, _ = validate(src, target)
            self.assertTrue(any("KEY_MULTIPLICITY" in x for x in errors))

    def test_alias_is_not_exact_input_product(self):
        with TemporaryDirectory() as d:
            src, target = Path(d) / "input.xlsx", Path(d) / "final.xlsx"
            write(src, ["Marca", "Modelo"], [["STIHL", "FS-120"]])
            write(target, ["Marca", "Modelo", "Título Real"],
                  [["STIHL", "FS120", "Another physical SKU"]])
            errors, _, _ = validate(src, target)
            self.assertTrue(any("KEY_MULTIPLICITY" in x for x in errors))


if __name__ == "__main__":
    unittest.main()
