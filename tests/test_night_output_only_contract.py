"""No-API regression for opt-in 20-SKU output-only overnight.

Checks that output-only mode is opt-in, requires review-before-promotion,
refuses any non-isolated classify context, and launches archived code using
the original history without modifying working-tree source.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pipeline_produtos as core


class NightOutputOnlyModeTests(unittest.TestCase):
    def test_cli_flag_defaults_off_and_explicit_is_on(self):
        args = core.build_parser().parse_args(["--mode", "overnight"])
        self.assertFalse(args.night_output_only)
        both = core.build_parser().parse_args([
            "--mode", "overnight", "--execute-live",
            "--night-output-only", "--campaign-review-before-promotion",
        ])
        self.assertTrue(both.night_output_only)
        self.assertTrue(both.campaign_review_before_promotion)

    def test_output_only_live_requires_explicit_review_gate_before_any_io(self):
        args = SimpleNamespace(
            execute_live=True, resume_dir="",
            campaign_total=20, campaign_resume_dir="",
            campaign_jev_budget=20, campaign_token_budget=100000,
            search_budget=20, campaign_pause_after_sublots=0,
            night_output_only=True, campaign_review_before_promotion=False,
        )
        with patch.object(core, "gate") as allowed:
            with self.assertRaisesRegex(ValueError, "NIGHT_OUTPUT_ONLY_REQUIRES_REVIEW"):
                core.overnight_campaign(args, {}, rules=None)
        allowed.assert_not_called()

    def test_output_only_cannot_run_on_non_isolated_source_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {"_nightly_output_only": True, "files": {
                "cache": str(root / "cache.sqlite"),
                "input": str(root / "input.xlsx"),
                "master": str(root / "master.xlsx"),
                "final": str(root / "final.xlsx"),
                "classification_db": str(root / "db.sqlite"),
                "output": str(root / "output.xlsx"),
                "inventory_output": str(root / "inventory.xlsx"),
            }}
            args = SimpleNamespace(
                mode="classify", cache="", input="", master="", final="",
                db="", output="", inventory_output="",
            )
            with self.assertRaisesRegex(RuntimeError, "OUTPUT_ONLY_ISOLATED_PATH_CONTRACT_FAILED"):
                core.run_pipeline(args, cfg, rules=None)

    def test_create_run_reads_reservations_from_live_history_not_snapshot_default(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            historical_root = root / "real_history"
            historical_root.mkdir()
            sources = {}
            for label in ("input", "cache", "master", "final", "classification_db"):
                path = root / (label + (".sqlite" if label in ("cache", "classification_db") else ".xlsx"))
                path.write_bytes(b"test-stub")
                sources[label] = path
            audited = {
                "candidate_rows": [{"brand": "ABC", "model": "ONE", "business_key": "abc|one"}],
                "error_rows": [], "summary": {}, "summary": {},
            }
            options = SimpleNamespace(
                allow_prior_serper_quota_error=False, search_budget=1,
                llm_budget=1, token_budget=2000, night_output_only=True,
            )
            with (patch.object(core, "OUT", historical_root),
                  patch.object(core, "source_paths", return_value=sources),
                  patch.object(core, "overnight_audit", return_value=audited),
                  patch.object(core, "snapshot", side_effect=shutil.copy2),
                  patch.object(core, "import_prior_reservations", return_value={}) as imported):
                run, _, isolated = core.create_run({"files": {}}, options)
            imported.assert_called_once()
            self.assertEqual(imported.call_args.args[1], historical_root)
            self.assertEqual(run.parent, historical_root)
            self.assertTrue(isolated["_nightly_output_only"])

    def test_output_only_review_manifest_and_resume_check_are_present(self):
        from inspect import getsource
        create_run_source = getsource(core.create_run)
        campaign_source = getsource(core.overnight_campaign)
        self.assertIn('"_nightly_output_only"', create_run_source)
        self.assertIn('"night_output_only"', campaign_source)
        self.assertIn('"review_before_promotion"', campaign_source)
        self.assertIn('Resume must preserve night-output-only mode', campaign_source)
        self.assertIn('not state.get("review_before_promotion", False)', campaign_source)


class NightSelectionRegressionTests(unittest.TestCase):
    def _sample(self):
        return {
            "candidate_rows": [
                {"business_key": "CLEAN", "brand": "A", "model": "1"},
            ],
            "missing_rows": [
                {"business_key": "MISSING", "brand": "B", "model": "2"},
            ],
            "error_rows": [
                {"business_key": "SERPER", "brand": "C", "model": "3",
                 "provider": "serper", "prior_state": "REQUEST_ATTEMPTED_NO_AUTO_RETRY",
                 "previous_error": "not enough credits"},
                {"business_key": "429", "brand": "D", "model": "4",
                 "provider": "tavily", "prior_state": "REQUEST_ERROR",
                 "previous_error": "429 too many requests"},
            ],
            "summary": {},
        }

    def test_raw_missing_audit_filters_approved_and_manual(self):
        rows = [
            SimpleNamespace(valid=True, key="A", brand_original="BR", model_original="A"),
            SimpleNamespace(valid=True, key="B", brand_original="BR", model_original="B"),
            SimpleNamespace(valid=True, key="C", brand_original="BR", model_original="C"),
        ]
        class Cache:
            def __init__(self, path):
                pass
            def get(self, brand, model):
                return SimpleNamespace(state="RAW_MISSING", payload={})
            def close(self):
                pass
        with (patch.object(core, "read_input_rows", return_value=rows),
              patch.object(core, "load_baseline_inventory", return_value={
                  "hard_lock": {"B"}, "processed": {"B", "C"}
              }),
              patch.object(core, "TavilyReadOnlyStore", Cache)):
            audit = core.overnight_audit(
                Path("input.xlsx"), Path("cache.sqlite"),
                Path("master.xlsx"), Path("final.xlsx")
            )
        self.assertEqual(
            [item["business_key"] for item in audit["missing_rows"]], ["A"]
        )
        self.assertEqual(audit["summary"]["raw_missing_hard_locked"], 1)
        self.assertEqual(audit["summary"]["raw_missing_processed"], 1)

    def test_unattempted_clean_only_is_default(self):
        eligible, details = core.select_overnight_eligible(self._sample(), set())
        self.assertEqual([x["business_key"] for x in eligible], ["CLEAN"])
        self.assertEqual(details["raw_missing_new_requires_opt_in"], 1)
        self.assertEqual(details["prior_provider_error_requires_opt_in"], 2)

    def test_explicit_permissions_unlock_missing_and_old_errors(self):
        eligible, details = core.select_overnight_eligible(
            self._sample(), set(), include_raw_missing=True,
            allow_previous_errors=True,
        )
        self.assertEqual([x["business_key"] for x in eligible],
                         ["CLEAN", "MISSING", "SERPER", "429"])
        self.assertEqual(details["eligible_unique"], 4)

    def test_serper_permission_is_narrow(self):
        eligible, _ = core.select_overnight_eligible(
            self._sample(), set(), allow_serper_quota_error=True,
        )
        self.assertEqual([x["business_key"] for x in eligible],
                         ["CLEAN", "SERPER"])

    def test_prior_attempts_never_reissued_even_with_all_permissions(self):
        eligible, details = core.select_overnight_eligible(
            self._sample(), {"CLEAN", "SERPER", "429", "MISSING"},
            include_raw_missing=True, allow_previous_errors=True,
            allow_serper_quota_error=True,
        )
        self.assertEqual(eligible, [])
        self.assertEqual(details["eligible_unique"], 0)

    def test_same_helper_used_in_live_run_and_launcher(self):
        from inspect import getsource
        self.assertIn("select_overnight_eligible(", getsource(core.create_run))
        source = (Path(__file__).resolve().parents[1] / "scripts"
                  / "launch_night_output_only.py").read_text(encoding="utf-8")
        self.assertIn("core.select_overnight_eligible(", source)


class ArchivedLauncherTests(unittest.TestCase):
    def test_launcher_default_does_not_invoke_live_runtime(self):
        source = Path(__file__).resolve().parents[1] / "scripts" / "launch_night_output_only.py"
        spec = importlib.util.spec_from_file_location("launch_no_api_probe", source)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "original"
            snapshot = root / "snapshot"
            (repo / "piloto_argentina" / "controlled_night").mkdir(parents=True)
            snapshot.mkdir()
            (snapshot / "pipeline_produtos.py").write_text("# offline fake", encoding="utf-8")
            files = {}
            for name in ("input", "cache", "master", "final", "classification_db"):
                path = repo / (name + ".xlsx")
                path.write_bytes(b"dryrun-stub")
                files[name] = path.name
            (repo / "pipeline_config.json").write_text(
                json.dumps({"files": files}), encoding="utf-8"
            )
            fake_audit = {"candidate_rows": [
                {"business_key": "test|%d" % i} for i in range(20)
            ], "error_rows": [], "summary": {}}
            argv = list(sys.argv)
            try:
                with (patch.object(mod, "SNAPSHOT", snapshot),
                      patch.object(core, "configured_slots", return_value=[object()]),
                      patch.object(core, "overnight_audit", return_value=fake_audit),
                      patch.object(core, "import_prior_reservations", return_value={}),
                      patch.object(core, "main") as paid):
                    sys.argv = ["launcher", "--repo", str(repo), "--target", "20"]
                    self.assertEqual(mod.main(), 0)
                    paid.assert_not_called()
                    self.assertFalse((snapshot / "night_output_only_local_config.json").exists())
            finally:
                sys.argv = argv

    def test_archived_code_preserves_real_history_and_config(self):
        source = Path(__file__).resolve().parents[1] / "scripts" / "launch_night_output_only.py"
        spec = importlib.util.spec_from_file_location("launch_night_output_only_test", source)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "original"
            snapshot = root / "snapshot"
            (repo / "piloto_argentina" / "controlled_night").mkdir(parents=True)
            repo.mkdir(exist_ok=True)
            snapshot.mkdir()
            (snapshot / "pipeline_produtos.py").write_text("# stand-in", encoding="utf-8")
            files = {}
            for label in ("input", "cache", "master", "final", "classification_db"):
                src = repo / (label + (".sqlite" if label in ("cache", "classification_db") else ".xlsx"))
                src.write_bytes(b"test-bytes")
                files[label] = src.name
            (repo / "pipeline_config.json").write_text(
                json.dumps({"files": files}), encoding="utf-8"
            )
            old_argv = list(sys.argv)
            previous_out = core.OUT
            try:
                fake_audit = {
                    "candidate_rows": [
                        {"business_key": "test|%d" % i} for i in range(20)
                    ],
                    "error_rows": [], "summary": {},
                }
                with (patch.object(mod, "SNAPSHOT", snapshot),
                      patch.object(core, "configured_slots", return_value=[object()]),
                      patch.object(core, "overnight_audit", return_value=fake_audit),
                      patch.object(core, "import_prior_reservations", return_value={}),
                      patch.object(core, "main", return_value=0) as runtime):
                    sys.argv = ["launcher", "--repo", str(repo), "--target", "20",
                                "--execute-live", "--include-raw-missing",
                                "--allow-reviewed-provider-errors",
                                "--allow-serper-quota-errors"]
                    self.assertEqual(mod.main(), 0)
                    runtime.assert_called_once()
                    self.assertIn("--night-include-raw-missing", sys.argv)
                    self.assertIn("--night-allow-previous-errors", sys.argv)
                    self.assertIn("--allow-prior-serper-quota-error", sys.argv)
                    self.assertEqual(core.OUT, repo / "piloto_argentina" / "controlled_night")
                    self.assertIn("--night-output-only", sys.argv)
                    self.assertIn("--campaign-review-before-promotion", sys.argv)
                    snap_cfg = json.loads((snapshot / "night_output_only_local_config.json").read_text(
                        encoding="utf-8"))
                    self.assertEqual(snap_cfg["files"]["master"], str(repo / files["master"]))
                    self.assertEqual(snap_cfg["files"]["final"], str(repo / files["final"]))
            finally:
                sys.argv = old_argv
                core.OUT = previous_out


if __name__ == "__main__":
    unittest.main()
