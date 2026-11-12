"""Regression for physical BI hard locks: exact Marca+Modelo, no normalized alias leaks.

Reordered approved rows must not count as changes. A changed human field
or an extra physical BI approval must block. No Search, JEV or real data.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

import pandas as pd

# unittest discover can import test modules without making tests/ a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_baseline_940_isolated_write import exact_human_rows


class ExactPhysicalHumanAuditTests(unittest.TestCase):
    def _rows(self):
        a = {
            "Marca": "ANNOVI REVERBERI", "Modelo": "52180",
            "O que é (Tipo)": "PUMP", "Alimentação": "CORDED",
            "Category General": "OTHERS", "FAMILY": "PUMP",
            "Título Real": "Original", "Status": "Manual",
            "Revisão": "VALIDADO_MANUAL", "Revisão B.I": "OK",
        }
        b = {
            **a, "Marca": "Annovi-Reverberi", "Título Real": "Alias human",
        }
        pending = {
            **a, "Marca": "ANNOVI REVERBERI", "Modelo": "UNREVIEWED",
            "Status": "Script", "Revisão": "REVISAR",
            "Revisão B.I": "",
        }
        return a, b, pending

    def test_reordered_normalized_aliases_preserve_every_human_row(self):
        a, b, pending = self._rows()
        with tempfile.TemporaryDirectory() as temp:
            original, generated = Path(temp) / "original.xlsx", Path(temp) / "generated.xlsx"
            pd.DataFrame([a, b, pending]).to_excel(original, index=False)
            pd.DataFrame([b, pending, a]).to_excel(generated, index=False)
            self.assertEqual(exact_human_rows(original), exact_human_rows(generated))

    def test_modified_approved_field_is_detected(self):
        a, b, pending = self._rows()
        with tempfile.TemporaryDirectory() as temp:
            original, generated = Path(temp) / "original.xlsx", Path(temp) / "generated.xlsx"
            pd.DataFrame([a, b, pending]).to_excel(original, index=False)
            changed = dict(b, FAMILY="PRESSURE WASHER")
            pd.DataFrame([a, changed, pending]).to_excel(generated, index=False)
            self.assertTrue(exact_human_rows(original) - exact_human_rows(generated))
            self.assertTrue(exact_human_rows(generated) - exact_human_rows(original))

    def test_unauthorized_bi_ok_inheritance_is_detected(self):
        a, b, pending = self._rows()
        with tempfile.TemporaryDirectory() as temp:
            original, generated = Path(temp) / "original.xlsx", Path(temp) / "generated.xlsx"
            pd.DataFrame([a, b, pending]).to_excel(original, index=False)
            newly_locked = dict(pending, **{"Revisão B.I": "OK"})
            pd.DataFrame([a, b, newly_locked]).to_excel(generated, index=False)
            self.assertEqual(sum((exact_human_rows(original) - exact_human_rows(generated)).values()), 0)
            self.assertEqual(sum((exact_human_rows(generated) - exact_human_rows(original)).values()), 1)

    def test_repeated_identical_physical_approval_is_preserved(self):
        a, _, pending = self._rows()
        with tempfile.TemporaryDirectory() as temp:
            original, generated = Path(temp) / "original.xlsx", Path(temp) / "generated.xlsx"
            pd.DataFrame([a, a, pending]).to_excel(original, index=False)
            pd.DataFrame([a, pending]).to_excel(generated, index=False)
            self.assertEqual(sum((exact_human_rows(original) - exact_human_rows(generated)).values()), 1)


if __name__ == "__main__":
    unittest.main()
