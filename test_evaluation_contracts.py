"""Offline regression checks for fail-closed classification evaluation."""
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from validate_v42_acceptance import validate


class EvaluationContracts(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.gold = Path(self.work.name) / "gold.csv"
        self.result = Path(self.work.name) / "result.xlsx"

    def run_eval(self, gold, result):
        pd.DataFrame(gold, columns=[
            "Marca", "Modelo", "Expected Category General", "Expected FAMILY"
        ]).to_csv(self.gold, index=False)
        pd.DataFrame(result, columns=[
            "Marca", "Modelo", "Category General", "FAMILY"
        ]).to_excel(self.result, index=False)
        return validate(self.result, self.gold)

    def gold_row(self, model="FS120", family="BRUSHCUTTER"):
        return {"Marca": "STIHL", "Modelo": model,
                "Expected Category General": "PETROL HANDHELD",
                "Expected FAMILY": family}

    def output_row(self, model="FS120", family="BRUSHCUTTER"):
        return {"Marca": "STIHL", "Modelo": model,
                "Category General": "PETROL HANDHELD", "FAMILY": family}

    def test_good_result_passes(self):
        report = self.run_eval([self.gold_row()], [self.output_row()])
        self.assertEqual(["SIM"], report["PASS"].tolist())

    def test_missing_result_does_not_pass(self):
        report = self.run_eval([self.gold_row()], [self.output_row(model="OTHER")])
        self.assertEqual(["MISSING_RESULT"], report["Failure"].tolist())

    def test_duplicate_result_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "RESULT_DUPLICATE_KEY"):
            self.run_eval([self.gold_row()], [
                self.output_row(), self.output_row(family="CHAINSAW")])

    def test_duplicate_gold_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "GOLD_DUPLICATE_KEY"):
            self.run_eval([self.gold_row(), self.gold_row()], [self.output_row()])

    def test_missing_gold_label_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "GOLD_MISSING_TAXONOMY"):
            self.run_eval([self.gold_row(family="")], [self.output_row()])

    def test_empty_gold_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "GOLD_EMPTY"):
            self.run_eval([], [self.output_row()])

    def test_empty_result_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "RESULT_EMPTY"):
            self.run_eval([self.gold_row()], [])


if __name__ == "__main__":
    unittest.main()
