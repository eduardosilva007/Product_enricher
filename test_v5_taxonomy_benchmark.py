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

    def test_taxonomy_aliases_are_canonicalized_conservatively(self):
        config = bt.load_taxonomy_normalization(
            HERE / "taxonomy_normalization.json"
        )
        aliases = bt.taxonomy_alias_map(config)
        self.assertEqual(
            ("WATERPUMPS", "WATERPUMP"),
            bt.canonical_pair("WATERPUMPS", "WATER PUMP", aliases),
        )
        self.assertEqual(
            ("OTHERS", "COMBO KIT"),
            bt.canonical_pair("OTHERS", "POWER TOOL COMBO KIT", aliases),
        )
        self.assertEqual(
            ("OTHERS", "ANGLE GRINDER"),
            bt.canonical_pair("OTHERS", "ANGLE GRINDER", aliases),
        )

    def test_others_others_is_catchall_not_actionable_taxonomy(self):
        config = bt.load_taxonomy_normalization(
            HERE / "taxonomy_normalization.json"
        )
        self.assertIn(
            "OTHERS || OTHERS",
            bt.catchall_labels(config),
        )

    def test_whitespace_is_normalized_before_alias_lookup(self):
        config = bt.load_taxonomy_normalization(
            HERE / "taxonomy_normalization.json"
        )
        aliases = bt.taxonomy_alias_map(config)
        self.assertEqual(
            ("OTHERS", "FAN BLADE"),
            bt.canonical_pair(" OTHERS ", " BLADE   FAN ", aliases),
        )

    def test_taxonomy_config_accepts_utf8_bom_on_windows(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "taxonomy.json"
            path.write_text(json.dumps({"pair_aliases": [], "catchall_pairs": [["OTHERS", "OTHERS"]]}),
                            encoding="utf-8-sig")
            loaded = bt.load_taxonomy_normalization(path)
            self.assertEqual(loaded["catchall_pairs"], [["OTHERS", "OTHERS"]])

    def test_tfidf_supports_singleton_taxonomy_classes(self):
        texts = [
            "BRAND A MODEL A1",
            "BRAND A MODEL A2",
            "BRAND B MODEL B1",
            "BRAND C MODEL C1",
        ]
        labels = np.array([
            "OTHERS || A",
            "OTHERS || A",
            "OTHERS || B",
            "OTHERS || C",
        ])
        estimator = bt.TfidfSVC().fit(texts, labels)
        X = estimator.transform(texts)
        proba = estimator.model.predict_proba(X)
        self.assertEqual((4, 3), proba.shape)
        np.testing.assert_allclose(proba.sum(axis=1), np.ones(4), atol=1e-8)

    def test_threshold_is_selected_on_validation(self):
        y = np.array(["A", "A", "B", "B"])
        pred = np.array(["A", "A", "B", "A"])
        conf = np.array([0.99, 0.95, 0.90, 0.60])
        threshold = bt.threshold_from_validation(y, pred, conf, 0.95)
        self.assertIsNotNone(threshold)
        self.assertGreaterEqual(threshold, 0.90)


if __name__ == "__main__":
    unittest.main()
