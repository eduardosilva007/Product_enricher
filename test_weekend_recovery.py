"""Recovery ledger prevents repeated paid work and advances only after successful persistence."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pipeline_produtos as p
from tools import weekend_cache_runner as runner


class WeekendRecoveryTests(unittest.TestCase):
    def args(self, folder: Path):
        return SimpleNamespace(cache="", master="", final="", max_items=2,
                               include_stale_auto=False, recovery_ledger=str(folder / "attempted.txt"),
                               llm_provider="jev", reprocess=False)

    def cfg(self, folder: Path):
        return {"files": {"cache": str(folder / "cache.sqlite"),
                          "master": str(folder / "master.xlsx"),
                          "final": str(folder / "final.xlsx"),
                          "pending_with_cache": str(folder / "with.xlsx"),
                          "pending_without_cache": str(folder / "without.xlsx")}}

    def test_ledger_advances_after_success_and_excludes_on_next_plan(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            args = self.args(folder)
            key = p.business_key("MAKITA", "HG6030")
            targets = [("MAKITA", "HG6030", key)]
            with patch.object(p, "TavilyReadOnlyStore"), \
                 patch.object(p, "build_cache_recovery_plan", return_value=(targets, {})) as plan, \
                 patch.object(p, "run_pipeline"):
                p.run_cache_recovery(args, self.cfg(folder), SimpleNamespace())
                self.assertEqual((folder / "attempted.txt").read_text().strip(), key)
                p.run_cache_recovery(args, self.cfg(folder), SimpleNamespace())
                self.assertEqual(plan.call_args.kwargs["exclude_keys"], {key})

    def test_ledger_does_not_advance_on_baseline_failure(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            args = self.args(folder)
            key = p.business_key("MAKITA", "HG6030")
            with patch.object(p, "TavilyReadOnlyStore"), \
                 patch.object(p, "build_cache_recovery_plan", return_value=([("MAKITA", "HG6030", key)], {})), \
                 patch.object(p, "run_pipeline", side_effect=RuntimeError("BASELINE_PERSISTENCE_FAILED")):
                with self.assertRaisesRegex(RuntimeError, "BASELINE_PERSISTENCE_FAILED"):
                    p.run_cache_recovery(args, self.cfg(folder), SimpleNamespace())
                self.assertFalse((folder / "attempted.txt").exists())

    def test_preflight_parser_understands_json(self):
        value = '24H_PREFLIGHT_OK: {"final_accepted": 9295, "missing_gold": 0, "sqlite": "ok"}'
        self.assertIsNotNone(runner.PREFLIGHT_RE.search(value))


if __name__ == "__main__":
    unittest.main()
