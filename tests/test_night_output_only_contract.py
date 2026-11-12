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

    def test_output_only_review_manifest_and_resume_check_are_present(self):
        from inspect import getsource
        create_run_source = getsource(core.create_run)
        campaign_source = getsource(core.overnight_campaign)
        self.assertIn('"_nightly_output_only"', create_run_source)
        self.assertIn('"night_output_only"', campaign_source)
        self.assertIn('"review_before_promotion"', campaign_source)
        self.assertIn('Resume must preserve night-output-only mode', campaign_source)
        self.assertIn('not state.get("review_before_promotion", False)', campaign_source)


class ArchivedLauncherTests(unittest.TestCase):
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
                with (patch.object(mod, "SNAPSHOT", snapshot),
                      patch.object(core, "configured_slots", return_value=[object()]),
                      patch.object(core, "main", return_value=0)):
                    sys.argv = ["launcher", "--repo", str(repo), "--target", "20"]
                    self.assertEqual(mod.main(), 0)
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
