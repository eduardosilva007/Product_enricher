"""Offline contract for all review-cache-evidence branches.

This does not invoke models, collections, or external providers.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReviewCacheEvidenceContractTests(unittest.TestCase):
    def test_every_review_cache_outcome_requires_review_and_blocks_auto(self):
        tree = ast.parse((ROOT / "pipeline_produtos.py").read_text(encoding="utf-8-sig"))
        stages = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            fields = {
                key.value: value.value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(value, ast.Constant)
            }
            if fields.get("Classification Outcome") != "REVIEW_CACHE_EVIDENCE":
                continue
            stage = fields.get("Stage")
            self.assertNotIn(stage, stages, f"Duplicate review stage: {stage}")
            stages[stage] = fields
        self.assertEqual(set(stages), {
            "COLLECTION_GATE",
            "SEMANTIC_FUNNEL_EMPTY",
            "CLEF_REPAIR_INSUFFICIENT",
            "CLEF_INSUFFICIENT",
        })
        for stage, fields in stages.items():
            with self.subTest(stage=stage):
                self.assertEqual(fields.get("Review Required"), "SIM")
                self.assertEqual(fields.get("Auto Eligible"), "N\u00c3O")
                self.assertEqual(fields.get("Resolution State"), "REVIEW_CACHE_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
