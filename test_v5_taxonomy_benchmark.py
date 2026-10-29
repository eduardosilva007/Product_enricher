import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent / "experiments" / "local_classifier_v5"
sys.path.insert(0, str(HERE))
import benchmark_taxonomy as bt


class V5TaxonomyBenchmarkContracts(unittest.TestCase):
    def test_model_family_core_groups_obvious_variants(self):
        self.assertEqual(bt.model_family_core("AA615/5/220"), bt.model_family_core("AA615/5/220 PLUS"))
        self.assertEqual(bt.model_family_core("TP413/6/220"), bt.model_family_core("TP413/7/220K"))

    def test_group_split_has_no_group_leakage(self):
        rows = []
        for i in range(80):
            rows.append({
                "__group": f"G{i//2}",
                "__label": "OTHERS || A" if i % 4 < 2 else "OTHERS || B",
            })
        df = pd.DataFrame(rows)
        train, val, test = bt.group_split(df, 0.15, 0.15)
        tg = set(df.iloc[train]["__group"])
        vg = set(df.iloc[val]["__group"])
        xg = set(df.iloc[test]["__group"])
        self.assertFalse(tg & vg)
        self.assertFalse(tg & xg)
        self.assertFalse(vg & xg)

    def test_threshold_is_selected_on_validation(self):
        y = np.array(["A", "A", "B", "B"])
        pred = np.array(["A", "A", "B", "A"])
        conf = np.array([0.99, 0.95, 0.90, 0.60])
        threshold = bt.threshold_from_validation(y, pred, conf, 0.95)
        self.assertIsNotNone(threshold)
        self.assertGreaterEqual(threshold, 0.90)


if __name__ == "__main__":
    unittest.main()
