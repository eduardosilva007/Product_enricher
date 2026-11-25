""""Processo completo" must run end to end: collect -> classify -> audit -> Final.

No separate main-menu step and no S/N prompt: the menu campaign promotes by itself
(dry-run first, fail-closed). Maintenance 9 keeps the manual GRAVAR recovery path.
"""
import contextlib
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline_produtos as pp

ROOT = Path(__file__).resolve().parents[1]


class MenuPromotionFlowTests(unittest.TestCase):
    def setUp(self):
        # Never depend on campaigns that exist on the machine running the tests.
        _p = patch.object(pp, "_night_incomplete_campaigns", return_value=[])
        _p.start()
        self.addCleanup(_p.stop)
    def test_main_menu_has_no_separate_promotion_step(self):
        with patch("builtins.input", side_effect=["5"]), self.assertRaises(ValueError):
            pp.interactive_menu(ROOT)

    def test_maintenance_9_keeps_manual_recovery(self):
        with patch("builtins.input", side_effect=["4", "9"]):
            args = pp.interactive_menu(ROOT)
        self.assertEqual(args.mode, "overnight-centralize")

    def test_menu_campaign_covers_previous_errors_and_credit_failures(self):
        with patch("builtins.input", side_effect=["3", "20", "1", "EXECUTAR"]):
            args = pp.interactive_menu(ROOT)
        self.assertTrue(args.night_allow_previous_errors)
        self.assertTrue(args.allow_prior_serper_quota_error)
        self.assertTrue(args.night_include_raw_missing)

    def test_auto_promotion_is_dry_run_then_commit_without_prompts(self):
        calls = []

        def fake(folder, cfg, rules, dry_run=False):
            calls.append(dry_run)
            return {"status": "DRY_RUN_PASS" if dry_run else "COMMITTED"}

        with patch.object(pp, "exclusive_campaign_lock", contextlib.nullcontext), \
             patch.object(pp, "_night_pending_centralization",
                          return_value=[(Path("c"), {"audited_products": 5})]), \
             patch.object(pp, "overnight_promote_campaign", side_effect=fake), \
             patch("builtins.input", side_effect=AssertionError("no prompt allowed")):
            self.assertEqual(pp.overnight_promote_pending_auto({}, object()), 0)
        self.assertEqual(calls, [True, False])

    def test_failed_dry_run_never_writes(self):
        calls = []

        def fake(folder, cfg, rules, dry_run=False):
            calls.append(dry_run)
            return {"status": "DRY_RUN_FAIL"}

        with patch.object(pp, "exclusive_campaign_lock", contextlib.nullcontext), \
             patch.object(pp, "_night_pending_centralization",
                          return_value=[(Path("c"), {})]), \
             patch.object(pp, "overnight_promote_campaign", side_effect=fake):
            with self.assertRaises(RuntimeError):
                pp.overnight_promote_pending_auto({}, object())
        self.assertEqual(calls, [True])

    def test_maintenance_item_5_is_still_base_repair(self):
        with patch("builtins.input", side_effect=["4", "5"]):
            args = pp.interactive_menu(ROOT)
        self.assertEqual(args.mode, "repair")

    def test_large_menu_campaign_is_marked_to_offer_next_step(self):
        with patch("builtins.input", side_effect=["3", "20", "1", "EXECUTAR"]):
            args = pp.interactive_menu(ROOT)
        self.assertEqual(args.mode, "overnight")
        self.assertTrue(args._from_menu)
        self.assertTrue(args.campaign_review_before_promotion)

    def _run_centralize(self, answers):
        calls = []

        def fake_promote(folder, cfg, rules, dry_run=False):
            calls.append(dry_run)
            return {"status": "DRY_RUN_PASS" if dry_run else "COMMITTED"}

        with patch.object(pp, "exclusive_campaign_lock", contextlib.nullcontext), \
             patch.object(pp, "_night_pending_centralization",
                          return_value=[(Path("campaign_x"), {"audited_products": 20})]), \
             patch.object(pp, "overnight_promote_campaign", side_effect=fake_promote), \
             patch("builtins.input", side_effect=answers):
            rc = pp.overnight_centralize_interactive({}, object())
        return rc, calls

    def test_promotion_requires_typed_gravar_after_dry_run(self):
        rc, calls = self._run_centralize(["", "GRAVAR"])
        self.assertEqual((rc, calls), (0, [True, False]))

    def test_anything_else_leaves_final_untouched(self):
        for answer in ("", "s", "gravar "[:-1].lower(), "N"):
            rc, calls = self._run_centralize(["", answer])
            self.assertEqual((rc, calls), (0, [True]), answer)

    def test_no_pending_campaign_is_a_noop(self):
        with patch.object(pp, "exclusive_campaign_lock", contextlib.nullcontext), \
             patch.object(pp, "_night_pending_centralization", return_value=[]), \
             patch.object(pp, "overnight_promote_campaign") as promote:
            self.assertEqual(pp.overnight_centralize_interactive({}, object()), 0)
        promote.assert_not_called()


class CentralStatePreparationTests(unittest.TestCase):
    def test_unknown_placeholder_is_fixable_real_difference_is_conflict(self):
        f = pp._central_field_action
        self.assertEqual(f("UNKNOWN", "CORDED"), "fix")
        self.assertEqual(f("", "CORDED"), "fix")
        self.assertEqual(f("CORDED", "CORDED"), "same")
        self.assertEqual(f("CORDLESS", "CORDED"), "conflict")

    def _prepare(self, statuses, auto=True):
        seen = []

        def fake_audit(cfg, commit=False):
            seen.append(commit)
            return {"status": statuses[len(seen) - 1]}

        with patch.object(pp, "exclusive_campaign_lock", contextlib.nullcontext), \
             patch.object(pp, "audit_committed_classifications", side_effect=fake_audit), \
             patch.object(pp, "overnight_promote_pending_auto") as promote, \
             patch.dict(pp.os.environ, {}, clear=False):
            pp.os.environ.pop("PIPELINE_CENTRAL_GATE_VERIFIED", None)
            rc = pp._prepare_central_state({}, object(), auto)
            flag = pp.os.environ.get("PIPELINE_CENTRAL_GATE_VERIFIED")
            pp.os.environ.pop("PIPELINE_CENTRAL_GATE_VERIFIED", None)
        return rc, seen, promote.call_count, flag

    def test_aligned_sets_flag_and_promotes_pending_first(self):
        self.assertEqual(self._prepare(["ALIGNED"]), (0, [False], 1, "1"))

    def test_restoration_required_is_restored_automatically(self):
        self.assertEqual(self._prepare(["RESTORATION_REQUIRED", "RESTORED_AND_AUDITED"]),
                         (0, [False, True], 1, "1"))

    def test_real_conflict_blocks_before_any_paid_call(self):
        rc, seen, _, flag = self._prepare(["BLOCKED_CONFLICT_OR_MISSING"])
        self.assertEqual((rc, seen, flag), (2, [False], None))


class PerformanceMemoTests(unittest.TestCase):
    def test_overnight_audit_runs_once_per_process_until_a_source_changes(self):
        import tempfile
        calls = []

        def fake(*paths):
            calls.append(1)
            return {"candidate_rows": [], "error_rows": [], "missing_rows": [], "summary": {}}

        with tempfile.TemporaryDirectory() as d:
            files = []
            for name in ("in.xlsx", "cache.sqlite", "master.xlsx", "final.xlsx"):
                f = Path(d) / name
                f.write_bytes(b"x")
                files.append(f)
            with patch.object(pp, "overnight_audit", side_effect=fake):
                pp._OVERNIGHT_AUDIT_MEMO.clear()
                first = pp.overnight_audit_cached(*files)
                second = pp.overnight_audit_cached(*files)
                self.assertIs(first, second)
                self.assertEqual(len(calls), 1)
                files[2].write_bytes(b"changed-size")  # a source changed
                pp.overnight_audit_cached(*files)
                self.assertEqual(len(calls), 2)
            pp._OVERNIGHT_AUDIT_MEMO.clear()

    def test_baseline_keys_memo_returns_independent_copies(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "b.xlsx"
            f.write_bytes(b"x")
            calls = []

            def fake(path):
                calls.append(1)
                return {"a"}, set(), set()

            with patch.object(pp, "_accepted_keys_from_baseline_file_uncached", side_effect=fake):
                first = pp._accepted_keys_from_baseline_file(f)
                first[0].add("mutated")
                second = pp._accepted_keys_from_baseline_file(f)
            self.assertEqual(second[0], {"a"})
            self.assertEqual(len(calls), 1)
            pp._BASELINE_KEYS_MEMO.clear()


class ScaleBlocksAndResumeTests(unittest.TestCase):
    def test_sublot_is_one_tavily_account_budget_and_budgets_scale(self):
        with patch("builtins.input", side_effect=["3", "13010", "1", "EXECUTAR"]), \
             patch.object(pp, "_night_incomplete_campaigns", return_value=[]):
            args = pp.interactive_menu(ROOT)
        self.assertEqual(args.search_budget, 1000)
        self.assertEqual(args.llm_budget, 1500)
        self.assertEqual(args.token_budget, 3_000_000)
        self.assertEqual(args.timeout_minutes, 1440)
        self.assertEqual(args.campaign_total, 13010)

    def test_blocks_are_written_to_final_before_the_next_block_starts(self):
        import types
        order, totals = [], []

        def fake_campaign(sub, cfg, rules=None):
            order.append("campaign")
            totals.append((sub.campaign_total, sub.campaign_jev_budget, sub.campaign_token_budget))
            return 0

        args = types.SimpleNamespace(campaign_total=4500, campaign_resume_dir="",
                                     campaign_jev_budget=0, campaign_token_budget=0)
        with patch.object(pp, "exclusive_campaign_lock", contextlib.nullcontext), \
             patch.object(pp, "overnight_campaign", side_effect=fake_campaign), \
             patch.object(pp, "overnight_promote_pending_auto",
                          side_effect=lambda c, r: order.append("promote") or 0):
            self.assertEqual(pp._run_menu_campaign_blocks(args, {}, object()), 0)
        self.assertEqual(order, ["campaign", "promote"] * 3)
        self.assertEqual([t[0] for t in totals], [2000, 2000, 500])
        self.assertEqual(totals[0][1:], (3000, 6000000))

    def test_failed_block_stops_without_promoting_or_starting_next(self):
        import types
        calls = []
        args = types.SimpleNamespace(campaign_total=4000, campaign_resume_dir="")
        with patch.object(pp, "exclusive_campaign_lock", contextlib.nullcontext), \
             patch.object(pp, "overnight_campaign", side_effect=lambda *a, **k: calls.append("c") or 2), \
             patch.object(pp, "overnight_promote_pending_auto") as promote:
            self.assertEqual(pp._run_menu_campaign_blocks(args, {}, object()), 2)
        self.assertEqual(calls, ["c"])
        promote.assert_not_called()

    def test_discarding_an_old_campaign_marks_it_and_starts_a_new_one(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "old"
            folder.mkdir()
            data = {"status": "RUNNING", "limits": {"target": 20}, "audited_products": 0}
            (folder / "campaign.json").write_text(json.dumps(data), encoding="utf-8")
            with patch.object(pp, "_night_incomplete_campaigns", return_value=[(folder, data)]), \
                 patch("builtins.input", side_effect=["3", "D", "20", "1", "EXECUTAR"]):
                args = pp.interactive_menu(ROOT)
            self.assertEqual(args.mode, "overnight")
            self.assertEqual(json.loads((folder / "campaign.json").read_text())["status"],
                             "ABANDONED_BY_USER")

    def test_menu_offers_resume_of_interrupted_campaign_with_same_authorizations(self):
        state = {"audited_products": 1000,
                 "limits": {"target": 2000, "search_per_sublot": 1000,
                            "jev_limit": 3000, "token_limit": 6000000},
                 "night_include_raw_missing": True, "night_allow_previous_errors": True,
                 "serper_rescue": True}
        with patch.object(pp, "_night_incomplete_campaigns",
                          return_value=[(Path("C:/x/campaigns/abc"), state)]), \
             patch("builtins.input", side_effect=["3", "S"]):
            args = pp.interactive_menu(ROOT)
        self.assertTrue(args._from_menu)
        self.assertTrue(args.campaign_resume_dir.endswith("abc"))
        self.assertEqual((args.campaign_total, args.search_budget,
                          args.campaign_jev_budget, args.campaign_token_budget),
                         (2000, 1000, 3000, 6000000))
        self.assertTrue(args.night_allow_previous_errors and args.night_include_raw_missing
                        and args.allow_prior_serper_quota_error)

    def test_only_cleanly_interrupted_campaigns_are_listed(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, status in (("a", "RUNNING"), ("b", "BLOCKED"), ("c", "COMPLETE")):
                (root / "campaigns" / name).mkdir(parents=True)
                (root / "campaigns" / name / "campaign.json").write_text(json.dumps(
                    {"status": status, "night_output_only": True, "tavily_pool": True}), encoding="utf-8")
            with patch.object(pp, "OUT", root):
                self.assertEqual([f.name for f, _ in pp._night_incomplete_campaigns()], ["a"])


if __name__ == "__main__":
    unittest.main()
