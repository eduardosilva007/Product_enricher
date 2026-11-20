"""Regression contract for centralized, Final-only overnight persistence.

Offline source-contract tests: no Search/JEV, Excel modifications or API calls.
The integration suite must separately validate staging and crash recovery.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
SHIM = (ROOT / "run_controlled_night_pilot.py").read_text(encoding="utf-8-sig")
TREE = ast.parse(SOURCE)
SHIM_TREE = ast.parse(SHIM)


def body(name, tree=TREE, source=SOURCE):
    fn = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.get_source_segment(source, fn)


class FinalOnlyPersistenceContract(unittest.TestCase):
    def test_pipeline_compiles(self):
        compile(SOURCE, "pipeline_produtos.py", "exec")
        compile(SHIM, "run_controlled_night_pilot.py", "exec")

    def test_staging_targets_only_final(self):
        stage = body("_night_stage_output_only_canonical")
        self.assertIn('for name in ("final",):', stage)
        self.assertNotIn('for name in ("master", "final"):', stage)

    def test_no_destructive_final_schema_collapse(self):
        stage = body("_night_stage_output_only_canonical")
        self.assertNotIn("_stage_only_business_columns_in_final(staged[name])", stage)
        self.assertIn("final_columns", stage)
        self.assertIn("_write_final_sku_index_to_stage", stage)

    def test_legacy_master_promotion_is_disabled(self):
        promote = body("overnight_promote_campaign")
        self.assertIn("FINAL_ONLY_PROMOTION_REQUIRED", promote)

    def test_commit_writes_only_final_and_technical_state(self):
        promote = body("overnight_promote_campaign")
        self.assertIn('for k in ("cache", "final", "classification_db"):', promote)
        self.assertNotIn('for k in ("cache", "master", "final", "classification_db"):\\n                os.replace', promote)
        self.assertIn('digest(canonical["master"]) != originals["master"]["sha256"]', promote)

    def test_protected_rows_and_backups_remain_gated(self):
        promote = body("overnight_promote_campaign")
        for token in ("PROMOTION_UNRELATED_ROW_CHANGED", "_overnight_assert_exact_human_locks",
                      "PROMOTION_SOURCE_CHANGED", "promotion_backup",
                      "PROMOTION_FINAL_MISSING", "PROMOTION_CACHE_VERIFICATION_FAILED"):
            with self.subTest(token=token):
                self.assertIn(token, promote)

    def test_old_cli_gets_bounded_safe_campaign(self):
        shim = body("main", SHIM_TREE, SHIM)
        for token in ("args.campaign_total = args.search_budget",
                      "args.campaign_jev_budget = args.llm_budget",
                      "args.campaign_token_budget = args.token_budget",
                      "args.campaign_review_before_promotion = True",
                      "args.night_output_only = True"):
            with self.subTest(token=token):
                self.assertIn(token, shim)


if __name__ == "__main__":
    unittest.main()
