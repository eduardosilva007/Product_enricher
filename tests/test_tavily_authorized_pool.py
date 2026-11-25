"""Offline authorization, budget and restart regressions for Tavily-only routing.

No network, API credits, Excel writes or LLM calls.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_produtos import DurableRouter, configured_slots, build_parser


class TavilyAuthorizedPoolTests(unittest.TestCase):

    def setUp(self):
        # Never depend on campaigns that exist on the machine running the tests.
        import pipeline_produtos as _pp
        from unittest.mock import patch as _patch
        _p = _patch.object(_pp, "_night_incomplete_campaigns", return_value=[])
        _p.start()
        self.addCleanup(_p.stop)
    def test_parser_accepts_explicit_pool_mode(self):
        args = build_parser().parse_args([
            "--mode", "collect", "--search-provider", "tavily",
            "--tavily-pool", "--max-search-new", "3",
        ])
        self.assertTrue(args.tavily_pool)
        self.assertFalse(args.auto_providers)
        self.assertEqual(args.max_tavily_new, 3)

    def test_full_pilot_skips_heavy_presync_and_requires_pool(self):
        source = (Path(__file__).resolve().parents[1] / "pipeline_produtos.py").read_text(encoding="utf-8-sig")
        self.assertIn('args.mode in {"collect", "full"} and args.tavily_pool', source)
        self.assertIn("TAVILY_POOL_REQUIRED", source)
        self.assertIn("TAVILY_POOL_NO_AUTHORIZED_BUDGET", source)

    def test_full_cohort_does_not_drop_collected_without_curated(self):
        code = (Path(__file__).resolve().parents[1] / "pipeline_produtos.py").read_text(
            encoding="utf-8-sig")
        self.assertIn('experimental_targets.append((brand, model, key))', code)
        self.assertNotIn('if (auto_route or provider in {"openserp", "scraperapi"}) and curated:', code)
        self.assertIn("FULL COLLECTION SCOPE | collected_targets=%d", code)
        self.assertIn("FULL CLASSIFICATION SCOPE | collected=%d", code)
        self.assertIn("FULL_COHORT_LIMIT_MISMATCH", code)

    def test_interactive_collect_directly_uses_tavily_pool(self):
        from pipeline_produtos import interactive_menu
        responses = iter(["1", "3"])
        with patch("builtins.input", side_effect=lambda *_: next(responses)):
            args = interactive_menu(Path("."))
        self.assertEqual(args.mode, "collect")
        self.assertEqual(args.search_provider, "tavily")
        self.assertTrue(args.tavily_pool)
        self.assertFalse(args.auto_providers)
        self.assertEqual(args.max_tavily_new, 3)

    def test_interactive_full_directly_uses_tavily_pool(self):
        from pipeline_produtos import interactive_menu
        responses = iter(["3", "3", "3", "3"])
        with patch("builtins.input", side_effect=lambda *_: next(responses)):
            args = interactive_menu(Path("."))
        self.assertEqual(args.mode, "full")
        self.assertEqual(args.search_provider, "tavily")
        self.assertTrue(args.tavily_pool)
        self.assertFalse(args.auto_providers)
        self.assertEqual(args.max_tavily_new, 3)
        self.assertEqual(args.max_items, 3)
        self.assertEqual(args.llm_provider, "local")

    def test_interactive_full_large_uses_internal_sublots_and_limits(self):
        from pipeline_produtos import interactive_menu
        responses = iter(["3", "50", "1", "EXECUTAR"])
        with patch("builtins.input", side_effect=lambda *_: next(responses)):
            args = interactive_menu(Path("."))
        self.assertEqual(args.mode, "overnight")
        self.assertTrue(args.execute_live)
        self.assertTrue(args.tavily_pool)
        self.assertEqual(args.campaign_total, 50)
        self.assertEqual(args.search_budget, 50)
        self.assertEqual(args.llm_budget, 75)
        self.assertEqual(args.token_budget, 150000)
        self.assertEqual(args.campaign_jev_budget, 75)
        self.assertEqual(args.campaign_token_budget, 150000)
        self.assertTrue(args.campaign_review_before_promotion)
        self.assertTrue(args.night_output_only)
        self.assertTrue(args.night_include_raw_missing)

    def test_interactive_full_large_requires_explicit_live_confirmation(self):
        from pipeline_produtos import interactive_menu
        responses = iter(["3", "50", "1", ""])
        with patch("builtins.input", side_effect=lambda *_: next(responses)):
            with self.assertRaises(SystemExit):
                interactive_menu(Path("."))

    def test_interactive_full_large_rejects_non_jev_unattended(self):
        from pipeline_produtos import interactive_menu
        responses = iter(["3", "50", "3"])
        with patch("builtins.input", side_effect=lambda *_: next(responses)):
            with self.assertRaisesRegex(ValueError, "OVERNIGHT_CAMPAIGN_REQUIRES_JEV"):
                interactive_menu(Path("."))

    def test_interactive_full_large_500_uses_same_internal_policy(self):
        from pipeline_produtos import interactive_menu
        responses = iter(["3", "500", "1", "EXECUTAR"])
        with patch("builtins.input", side_effect=lambda *_: next(responses)):
            args = interactive_menu(Path("."))
        self.assertEqual(args.campaign_total, 500)
        self.assertEqual(args.search_budget, 500)
        self.assertEqual(args.campaign_jev_budget, 750)
        self.assertEqual(args.campaign_token_budget, 1500000)
        self.assertTrue(args.night_include_raw_missing)

    def test_missing_raw_only_selected_when_explicitly_enabled(self):
        from pipeline_produtos import select_overnight_eligible
        audit = {"candidate_rows": [], "error_rows": [], "missing_rows": [
            {"business_key": "NEW-1", "brand": "A", "model": "B"},
            {"business_key": "HISTORICAL", "brand": "A", "model": "C"},
        ]}
        eligible_default, _ = select_overnight_eligible(audit, {"HISTORICAL"})
        self.assertEqual(eligible_default, [])
        eligible_menu, summary = select_overnight_eligible(
            audit, {"HISTORICAL"}, include_raw_missing=True
        )
        self.assertEqual([row["business_key"] for row in eligible_menu], ["NEW-1"])
        self.assertEqual(summary["raw_missing_new_previous_campaign"], 1)
        self.assertEqual(summary["raw_missing_new_selected"], 1)

    def test_overnight_dry_run_reports_eligible_not_only_raw_empty(self):
        source = (Path(__file__).resolve().parents[1] / "pipeline_produtos.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('"eligible": len(eligible)', source)
        self.assertIn('"raw_missing_input": len(report.get("missing_rows", []))', source)

    def test_twenty_tavily_slots_and_other_providers_can_be_excluded(self):
        env = {f"TAVILY_API_KEY_{i}": f"offline-key-{i}" for i in range(1, 21)}
        env.update({f"TAVILY_LIMIT_{i}": "2" for i in range(1, 21)})
        env.update({"SERPER_API_KEY_1": "unused", "SERPER_LIMIT_1": "2"})
        with patch.dict(os.environ, env, clear=True):
            authorized = [slot for slot in configured_slots() if slot.provider == "tavily"]
        self.assertEqual(len(authorized), 20)
        self.assertEqual([slot.account for slot in authorized], [f"tavily:{i}" for i in range(1, 21)])

    def test_budget_switches_on_next_sku_not_retry_of_same_sku(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "TAVILY_API_KEY_1": "offline-key-1", "TAVILY_LIMIT_1": "1",
                "TAVILY_API_KEY_2": "offline-key-2", "TAVILY_LIMIT_2": "1",
            }
            with patch.dict(os.environ, env, clear=True):
                slots = [s for s in configured_slots() if s.provider == "tavily"]
            path = Path(tmp) / "attempts.sqlite"
            router = DurableRouter(path, slots)
            try:
                self.assertEqual(router.reserve("SKU-A").account, "tavily:1")
                self.assertIsNone(router.reserve("SKU-A"))
                self.assertEqual(router.reserve("SKU-B").account, "tavily:2")
                self.assertIsNone(router.reserve("SKU-C"))
            finally:
                router.close()
            resumed = DurableRouter(path, slots)
            try:
                self.assertTrue(resumed.already_reserved("SKU-A"))
                self.assertTrue(resumed.already_reserved("SKU-B"))
                self.assertIsNone(resumed.reserve("SKU-C"))
            finally:
                resumed.close()
            # sqlite3.Connection.__exit__ commits/rolls back but does NOT close.
            # Windows prevents TemporaryDirectory cleanup while the DB handle is open.
            from contextlib import closing
            with closing(sqlite3.connect(path)) as db:
                count = db.execute("SELECT COUNT(*) FROM collection_attempt_reservations").fetchone()[0]
            self.assertEqual(count, 2)

    def test_billing_group_shared_budget_not_multiplied_by_keys(self):
        env = {
            "TAVILY_API_KEY_1": "offline-key-1", "TAVILY_LIMIT_1": "1",
            "TAVILY_API_KEY_2": "offline-key-2", "TAVILY_LIMIT_2": "1",
            "TAVILY_BILLING_GROUP_1": "same-authorized-billing-group",
            "TAVILY_BILLING_GROUP_2": "same-authorized-billing-group",
        }
        with patch.dict(os.environ, env, clear=True):
            slots = [s for s in configured_slots() if s.provider == "tavily"]
        with tempfile.TemporaryDirectory() as tmp:
            router = DurableRouter(Path(tmp) / "attempts.sqlite", slots)
            try:
                self.assertEqual(router.reserve("SKU-A").account, "tavily:1")
                self.assertIsNone(router.reserve("SKU-B"))
            finally:
                router.close()

    def test_sha256_baseline_fingerprint_invalidates_on_any_change(self):
        from pipeline_produtos import (
            _baseline_signature, _baseline_sync_manifest_check,
            _baseline_sync_manifest_store,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ("input", "master", "final", "cache", "config", "taxonomy", "collection_policy")
            paths = {name: root / (name + (".sqlite" if name == "cache" else ".dat")) for name in names}
            for name, path in paths.items():
                path.write_bytes(name.encode("ascii"))
            manifest = root / "state.json"
            first = _baseline_signature(paths, "rules-v1")
            self.assertTrue(first)
            self.assertFalse(_baseline_sync_manifest_check(manifest, first))
            _baseline_sync_manifest_store(manifest, first)
            self.assertTrue(_baseline_sync_manifest_check(manifest, first))
            paths["final"].write_bytes(b"new-manual-BI-value")
            self.assertFalse(_baseline_sync_manifest_check(
                manifest, _baseline_signature(paths, "rules-v1")))
            paths["final"].write_bytes(b"final")
            paths["cache"].with_name("cache.sqlite-wal").write_bytes(b"new-cache-write")
            self.assertFalse(_baseline_sync_manifest_check(
                manifest, _baseline_signature(paths, "rules-v1")))
            self.assertFalse(_baseline_sync_manifest_check(
                manifest, _baseline_signature(paths, "rules-v2")))

    def test_fingerprint_fail_closed_without_workbooks(self):
        from pipeline_produtos import _baseline_signature
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(_baseline_signature({
                "input": root / "missing.xlsx",
                "master": root / "master.xlsx",
                "final": root / "final.xlsx",
            }, "rule-hash"))

    def test_explicit_new_period_keeps_old_sku_reservations(self):
        from datetime import datetime, timezone
        today_utc = datetime.now(timezone.utc).date().isoformat()
        env = {
            "TAVILY_API_KEY_1": "offline-key-1",
            "TAVILY_LIMIT_1": "2",
            "TAVILY_BUDGET_SINCE_1": today_utc,
        }
        with patch.dict(os.environ, env, clear=True):
            slots = [s for s in configured_slots() if s.provider == "tavily"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.sqlite"
            router = DurableRouter(path, slots)
            try:
                router.conn.execute(
                    "INSERT INTO collection_attempt_reservations "
                    "(business_key,account,provider,reserved_at) VALUES (?,?,?,?)",
                    ("SKU-LEGACY", "tavily:1", "tavily", "2026-11-15 06:10:48"),
                )
                router.conn.commit()
                self.assertIsNone(router.reserve("SKU-LEGACY"))
                self.assertEqual(router.reserve("SKU-NEW").account, "tavily:1")
            finally:
                router.close()
            from contextlib import closing
            with closing(sqlite3.connect(path)) as db:
                total = db.execute("SELECT COUNT(*) FROM collection_attempt_reservations").fetchone()[0]
            self.assertEqual(total, 2)

    def test_invalid_budget_period_rejected(self):
        with patch.dict(os.environ, {
            "TAVILY_API_KEY_1": "offline-key",
            "TAVILY_LIMIT_1": "2",
            "TAVILY_BUDGET_SINCE_1": "yesterday",
        }, clear=True):
            with self.assertRaisesRegex(ValueError, "Invalid budget date"):
                configured_slots()

    def test_disabled_account_remains_disabled_after_restart(self):
        env = {
            "TAVILY_API_KEY_1": "offline-key-1", "TAVILY_LIMIT_1": "2",
            "TAVILY_API_KEY_2": "offline-key-2", "TAVILY_LIMIT_2": "2",
        }
        with patch.dict(os.environ, env, clear=True):
            slots = [s for s in configured_slots() if s.provider == "tavily"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.sqlite"
            router = DurableRouter(path, slots)
            try:
                router.disable("tavily:1", "TAVILY_LIMIT")
            finally:
                router.close()
            restarted = DurableRouter(path, slots)
            try:
                self.assertEqual(restarted.reserve("SKU-A").account, "tavily:2")
            finally:
                restarted.close()


if __name__ == "__main__":
    unittest.main()
