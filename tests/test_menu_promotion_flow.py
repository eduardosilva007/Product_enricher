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


if __name__ == "__main__":
    unittest.main()
