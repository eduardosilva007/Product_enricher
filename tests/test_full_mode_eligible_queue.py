"""Full mode must limit pending input SKUs, never the historical cache prefix."""
import unittest
from types import SimpleNamespace

from pipeline_produtos import (
    _use_input_scoped_evidence_cohort, build_classification_cohort,
    build_pilot_cohort, business_key,
)


class FakeCache:
    def __init__(self, products):
        self.products = products

    def catalog(self):
        return list(self.products)

    def get(self, brand, model):
        return SimpleNamespace(
            state="RAW_OK",
            payload={"_collection": {
                "state": "READY_FOR_CLASSIFICATION",
                "curated_results": [{"title": "Verified product result"}],
            }},
        )


class FullModeTargetSelectionTests(unittest.TestCase):
    def setUp(self):
        self.products = [
            ("BRAND", f"MODEL-{i:04d}", business_key("BRAND", f"MODEL-{i:04d}"))
            for i in range(1200)
        ]
        self.processed = {row[2] for row in self.products[:1030]}
        self.cache = FakeCache(self.products)

    def test_full_and_classify_use_same_input_scoped_eligible_queue(self):
        self.assertTrue(_use_input_scoped_evidence_cohort("full"))
        self.assertTrue(_use_input_scoped_evidence_cohort("classify"))
        self.assertFalse(_use_input_scoped_evidence_cohort("recover"))

    def test_limit_applies_after_skipping_history(self):
        _, old_targets, _, _ = build_pilot_cohort(
            self.products, processed=self.processed, manual=set(), max_items=1030)
        self.assertEqual(len(old_targets), 0)
        _, targets, stats, _ = build_classification_cohort(
            self.cache, processed=self.processed, manual=set(),
            hard_lock=set(), max_items=1030,
            allowed_keys={x[2] for x in self.products},
        )
        self.assertEqual(len(targets), 170)
        self.assertEqual(stats["cohort_already_classified"], 1030)
        self.assertEqual(targets[0][2], self.products[1030][2])

    def test_filtered_input_excludes_unrelated_history(self):
        only_five = {x[2] for x in self.products[-5:]}
        _, targets, stats, _ = build_classification_cohort(
            self.cache, processed=self.processed, manual=set(),
            hard_lock=set(), max_items=1030, allowed_keys=only_five,
        )
        self.assertEqual(len(targets), 5)
        self.assertEqual(stats["classification_outside_input_skipped"], 1195)

    def test_human_lock_remains_enforced(self):
        allowed = {x[2] for x in self.products}
        hard = {self.products[-1][2]}
        _, targets, stats, _ = build_classification_cohort(
            self.cache, processed=self.processed, manual=set(),
            hard_lock=hard, max_items=1030, allowed_keys=allowed,
        )
        self.assertEqual(len(targets), 169)
        self.assertEqual(stats["cohort_bi_ok_lock"], 1)


if __name__ == "__main__":
    unittest.main()
