import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook
from tools.final_output_gate import validate


def write_excel(path, headers, records):
    wb = Workbook()
    sheet = wb.active
    sheet.append(headers)
    for row in records:
        sheet.append(row)
    wb.save(path)
    wb.close()


class FinalOutputGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "input.xlsx"
        self.output = root / "final.xlsx"
        self.columns = ["Marca", "Modelo", "Título Referência", "Fonte URL", "Resumo Evidências"]

    def check(self, sources, outputs, output_headers=None):
        write_excel(self.source, self.columns, sources)
        write_excel(self.output, output_headers or
                    ["Marca", "Modelo", "Título Referência", "Título Real", "Fonte URL", "Resumo Evidências"], outputs)
        return validate(self.source, self.output)[0]

    def test_retains_manual_reference_and_row(self):
        errors = self.check(
            [["STIHL", "FS120", "STIHL FS 120", "https://example.com", "Brushcutter"]],
            [["STIHL", "FS120", "STIHL FS 120", "STIHL FS 120", "https://example.com", "Brushcutter"]])
        self.assertEqual([], errors)

    def test_missing_input_product_is_blocked(self):
        errors = self.check(
            [["STIHL", "FS120", None, None, None], ["HONDA", "GX", None, None, None]],
            [["STIHL", "FS120", None, None, None, None]])
        self.assertTrue(any("ROW_COUNT" in x or "KEY_MULTIPLICITY" in x for x in errors))

    def test_rejected_source_reference_cannot_disappear(self):
        errors = self.check(
            [["STIHL", "FS120", "Possible FS120 manual", "https://example.org", "Source snippet"]],
            [["STIHL", "FS120", None, None, None, None]])
        self.assertTrue(any("LOST_REFERENCE_DATA" in x for x in errors))

    def test_duplicate_multiplicity_is_preserved(self):
        errors = self.check(
            [["A", "1", None, None, None], ["A", "1", None, None, None]],
            [["A", "1", None, None, None, None]])
        self.assertTrue(any("KEY_MULTIPLICITY" in x for x in errors))

    def test_reference_title_requires_review_visible_real_title(self):
        errors = self.check(
            [["A", "1", None, None, None]],
            [["A", "1", "Found title", None, None, None]])
        self.assertTrue(any("REFERENCE_TITLE_NOT_IN_REAL_TITLE" in x for x in errors))


if __name__ == "__main__":
    unittest.main()
